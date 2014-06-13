###############################################################################
##
##  Copyright (C) 2013 Tavendo GmbH
##
##  Licensed under the Apache License, Version 2.0 (the "License");
##  you may not use this file except in compliance with the License.
##  You may obtain a copy of the License at
##
##      http://www.apache.org/licenses/LICENSE-2.0
##
##  Unless required by applicable law or agreed to in writing, software
##  distributed under the License is distributed on an "AS IS" BASIS,
##  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
##  See the License for the specific language governing permissions and
##  limitations under the License.
##
###############################################################################

import json
from collections import deque

from twisted.python import log
from twisted.web.resource import Resource, NoResource

## Each of the following 2 trigger a reactor import at module level
from twisted.web import http
from twisted.web.server import NOT_DONE_YET

from autobahn.util import newid


from websocket import WampWebSocketProtocol, parseSubprotocolIdentifier, WampWebSocketServerProtocol
from serializer import JsonSerializer

from autobahn.websocket.protocol import WebSocketProtocol
import traceback
import message
from autobahn.wamp.exception import TransportLost
from twisted.web.server import Site
from twisted.web.static import File
from autobahn.twisted.resource import WebSocketResource
from autobahn.twisted.websocket import WampWebSocketServerFactory

class WampHttpWebsocketServerFactory(Site, WampWebSocketServerFactory):

   def __init__(self, factory,
                serializers = None,
                timeout = 10,
                killAfter = 30,
                queueLimitBytes = 128 * 1024,
                queueLimitMessages = 100,
                debug = False,
                reactor = None,
                allowed_origins = "*"):

      self._ws_factory = WampWebSocketServerFactory(factory, debug_wamp = debug, serializers = serializers)

      self._root = File("longpoll")
      self._resource = WampHttpResource(self._ws_factory, serializers, debug=debug, timeout=timeout, killAfter=killAfter,
                                        queueLimitBytes = queueLimitBytes, queueLimitMessages = queueLimitMessages,
                                        reactor = reactor, allowed_origins=allowed_origins)
      self._root.putChild("ws", WebSocketResource(self._ws_factory))
      self._root.putChild("longpoll", self._resource)
      Site.__init__(self,self._root)

   def setProtocol(self, protocol):
      self._ws_factory.protocol = protocol

   def setProtocolOptions(self, **kwargs):

      self._ws_factory.setProtocolOptions(**kwargs)
   def log(self, *args, **kwargs):
      pass

class WampHttpSecurity:

   def _restrictToIp(self, request, transportid=None, parent=None):
      allowed_ip = None
      if transportid is None:
         transportid = self._parent._transportid
      if parent is None:
         parent = self._parent._parent
      if transportid in parent._ips:
         allowed_ip = parent._ips[transportid]

      if allowed_ip != request.client.host:

         request.setHeader('content-type', 'text/plain; charset=UTF-8')
         request.setResponseCode(http.BAD_GATEWAY)

         raise Exception("ip not allowed to access transport")

   def _revokeIpAccess(self,request,transport):
      if not hasattr(self._parent,"_ips"):
         self._parent._ips = {}
      if transport in self._parent._ips:
         del self._parent._ips[transport]

   def _grantIpAccess(self,request,transport):
      if not hasattr(self._parent,"_ips"):
         self._parent._ips = {}
      self._parent._ips[transport] = request.client.host
class WampHttpResourceCors:


   def setCorsHeaders(self, request):
      """
      Set standard HTTP response headers.
      """
      origin = request.getHeader("Origin")
      if origin is None or origin == "null":
         origin = "*"
      allowed_origin = self._allowed_origins

      if not type(allowed_origin) is list:
         allowed_origin = [allowed_origin]



      request.setHeader('access-control-allow-origin', ", ".join(allowed_origin))
      request.setHeader('access-control-allow-credentials', 'true')
      request.setHeader('access-control-allow-methods', 'POST, OPTIONS')
      request.setHeader('Access-Control-Request-Method', 'POST')
      request.setHeader('Access-Control-Max-Age', 1728000)

      headers = request.getHeader('Access-Control-Request-Headers')
      if headers is not None:
         request.setHeader('Access-Control-Allow-Headers', headers)
   def render_OPTIONS(self, request):
      self.setCorsHeaders(request)
      return ""
class WampHttpResourceSessionSend(Resource, WampHttpSecurity, WampHttpResourceCors):
   """
   A Web resource for sending via XHR that is part of a WampHttpResourceSession.
   """

   def __init__(self, parent, allowed_origins):
      """
      """
      Resource.__init__(self)

      self._parent = parent
      self._debug = self._parent._parent._debug
      self.reactor = self._parent.reactor
      self._allowed_origins = allowed_origins


   def render_POST(self, request):
      """
      WAMP message send.
      """

      self._restrictToIp(request)
      payload = request.content.read()
      try:
         if self._debug:
            log.msg("WAMP session data received (transport ID %s): %s" % (self._parent._transportid, payload))
         self._parent.onMessage(payload, False)
      except TransportLost as tl:
          raise tl
      except Exception as e:
         request.setHeader('content-type', 'text/plain; charset=UTF-8')
         request.setResponseCode(http.BAD_REQUEST)
         return "could not unserialize WAMP message [%s]" % e

      request.setResponseCode(http.NO_CONTENT)
      self._parent._parent.setStandardHeaders(request)
      self.setCorsHeaders(request)
      request.setHeader('content-type', 'application/json; charset=utf-8')

      self._parent._isalive = True
      return ""



class WampHttpResourceSessionReceive(Resource, WampHttpSecurity, WampHttpResourceCors):
   """
   A Web resource for receiving via XHR that is part of a WampHttpResourceSession.
   """

   def __init__(self, parent, allowed_origins):
      """
      """
      Resource.__init__(self)

      self._parent = parent
      self._allowed_origins = allowed_origins
      self._debug = self._parent._parent._debug
      self.reactor = self._parent._parent.reactor

      self._queue = deque()
      self._request = None
      self._killed = False

      if self._debug:
         def logqueue():
            if not self._killed:
               log.msg("WAMP session send queue length (transport ID %s): %s" % (self._parent._transportid, len(self._queue)))
               if not self._request:
                  log.msg("WAMP session has no XHR poll request (transport ID %s)" % self._parent._transportid)
               self.reactor.callLater(1, logqueue)
         logqueue()


   def queue(self, data):
      self._queue.append(data)
      self._trigger()


   def _kill(self):
      if self._request:
         self._request.finish()
         self._request = None
      self._killed = True


   def _trigger(self):
      if self._request and len(self._queue):

         ## batched sending of queued messages
         ##
         self._request.write('[')

         while len(self._queue) > 0:
            msg = self._queue.popleft()
            self._request.write(msg)
            if len(self._queue):
               self._request.write(',')

         self._request.write(']')

         self._request.finish()
         self._request = None
         return True
      #else:
      #   self._request.setResponseCode(http.REQUEST_TIMEOUT)

      return False


   def render_POST(self, request):
      self._restrictToIp(request)
      self._parent._parent.setStandardHeaders(request)
      self.setCorsHeaders(request)
      request.setHeader('content-type', 'application/json; charset=utf-8')

      self._request = request

      def cancel(err):
         if self._debug:
            log.msg("WAMP session XHR poll request gone (transport ID %s" % self._parent._transportid)
         self._request.setResponseCode(http.REQUEST_TIMEOUT)
         self._request = None

      request.notifyFinish().addErrback(cancel)

      self._parent._isalive = True
      self._trigger()


      return NOT_DONE_YET



class WampHttpResourceSession(Resource, WampWebSocketProtocol, WebSocketProtocol):
   """
   A Web resource representing an open WAMP session.
   """

   def __init__(self, parent, transportid, serializer, allowed_origins):
      """
      Create a new Web resource representing a WAMP session.

      :param parent: The WAMP Web base resource.
      :type parent: Instance of WampHttpResource.
      :param serializer: The WAMP serializer in use for this session.
      :type serializer: An instance of WampSerializer.
      """
      Resource.__init__(self)
      self._parent = parent
      self.factory = self._parent.factory
      self._debug = self.debugCodePaths = self._parent._debug
      self.failByDrop = False
      self.reactor = self._parent.reactor
      self._authenticated = None
      self._transportid = transportid
      self._serializer = serializer
      self._send = WampHttpResourceSessionSend(self, allowed_origins)
      self._receive = WampHttpResourceSessionReceive(self, allowed_origins)
      self.putChild("send", self._send)
      self.putChild("receive", self._receive)


      killAfter = self._parent._killAfter
      self._isalive = False

      def killIfDead():
         if not self._isalive:
            if self._debug:
               log.msg("killing inactive WAMP session (transport ID %s)" % self._transportid)

            if self._session and self._session._session_id:
               self._session.leave(u"wamp.close.timeout", unicode("Session inactive"))
            else:
               self.failConnection(5000, "Session inactive")
            #self.onClose(False, 5000, "Session inactive")
            #self._receive._kill()
            #del self._parent._transports[self._transportid]

         else:
            if self._debug:
               log.msg("WAMP session still alive (transport ID %s)" % self._transportid)
            #self.sendHeartbeat()
            self._isalive = False
            self.reactor.callLater(killAfter, killIfDead)

      self.reactor.callLater(killAfter, killIfDead)

      if self._debug:
         log.msg("WAMP session resource initialized (transport ID %s)" % self._transportid)

      self.onOpen()

   def sendHeartbeat(self):
      self._session.sendHeartbeat()
   def onClose(self,  wasClean, code, reason):
      WampWebSocketProtocol.onClose(self, wasClean, code, reason)
      self._receive._kill()
      del self._parent._transports[self._transportid]

   def sendMessage(self, bytes, isBinary):
      if self._debug:
         log.msg("WAMP session send bytes (transport ID %s): %s" % (self._transportid, bytes))
      self._receive.queue(bytes)

   def failConnection(self, code = WebSocketProtocol.CLOSE_STATUS_CODE_GOING_AWAY, reason = "Going Away"):
       if self._session:
           if self._session._session_id is None:

              msg = message.Abort(reason = unicode(reason), message = unicode(reason))
              self.send(msg)
              del self._parent._transports[self._transportid]
           else:
              self._session._closing_down = True
              msg = message.Goodbye(reason = unicode(reason), message = unicode(reason))
              self.send(msg)

       else:
          pass


class WampHttpResourceClose(Resource, WampHttpSecurity, WampHttpResourceCors):
   """
   A Web resource for creating new WAMP sessions.
   """

   def __init__(self, parent, allowed_origins):
      """
      """
      Resource.__init__(self)
      self._parent = parent
      self._debug = self._parent._debug
      self.reactor = self._parent.reactor
      self._allowed_origins = allowed_origins


   def _failRequest(self, request, msg):
      request.setHeader('content-type', 'text/plain; charset=UTF-8')
      request.setResponseCode(http.BAD_REQUEST)
      return msg



   def render_POST(self, request):
      """
      Request to create a new WAMP session.
      """
      transportid=request.args["session"][0]
      self._restrictToIp(request, transportid=transportid,parent=self._parent)

      self._revokeIpAccess(request, transportid)

      self._parent._transports[transportid].delEntity("send")
      self._parent._transports[transportid].children["receive"]._kill()
      self._parent._transports[transportid].delEntity("receive")
      del self._parent._transports[transportid]
      return ""



class WampHttpResourceOpen(Resource, WampHttpSecurity, WampHttpResourceCors):
   """
   A Web resource for creating new WAMP sessions.
   """

   def __init__(self, parent, allowed_origins = None):
      """
      """
      Resource.__init__(self)
      self._parent = parent
      self._debug = self._parent._debug
      self.reactor = self._parent.reactor
      self._allowed_origins = allowed_origins


   def _failRequest(self, request, msg):
      request.setHeader('content-type', 'text/plain; charset=UTF-8')
      request.setResponseCode(http.BAD_REQUEST)
      return msg


   def render_POST(self, request):
      """
      Request to create a new WAMP session.
      """
      self._parent.setStandardHeaders(request)
      self.setCorsHeaders(request)
      payload = request.content.read()

      try:
         options = json.loads(payload)
      except Exception as e:
         return self._failRequest(request, "could not parse WAMP session open request body [%s]" % e)

      if type(options) != dict:
         return self._failRequest(request, "invalid type for WAMP session open request [was '%s', expected dictionary]" % type(options))

      if not options.has_key('protocols'):
         return self._failRequest(request, "missing attribute 'protocols' in WAMP session open request")

      protocol = None
      for p in options['protocols']:
         version, serializerId = parseSubprotocolIdentifier(p)
         if version == 2 and serializerId in self._parent._serializers.keys():
            serializer = self._parent._serializers[serializerId]
            protocol = p
            break

      request.setHeader('content-type', 'application/json; charset=utf-8')

      transportid = newid()

      ## create instance of WampHttpResourceSession or subclass thereof ..
      ##

      self._parent._transports[transportid] = self._parent.protocol(self._parent, transportid, serializer, self._allowed_origins)
      self._grantIpAccess(request, transportid)
      #self._parent.state = self._parent.STATE_OPEN
      ret = {'transport': transportid, 'protocol': protocol}

      return json.dumps(ret)



class WampHttpResource(Resource):
   """
   A WAMP Web base resource.
   """

   protocol = WampHttpResourceSession


   def __init__(self,
                factory,
                serializers = None,
                timeout = 10,
                killAfter = 30,
                queueLimitBytes = 128 * 1024,
                queueLimitMessages = 100,
                debug = False,
                reactor = None,
                allowed_origins = None):
      """
      Create new HTTP WAMP Web resource.

      :param serializers: List of WAMP serializers.
      :type serializers: List of WampSerializer objects.
      :param timeout: XHR polling timeout in seconds.
      :type timeout: int
      :param killAfter: Kill WAMP session after inactivity in seconds.
      :type killAfter: int
      :param queueLimitBytes: Kill WAMP session after accumulation of this many bytes in send queue (XHR poll).
      :type queueLimitBytes: int
      :param queueLimitMessages: Kill WAMP session after accumulation of this many message in send queue (XHR poll).
      :type queueLimitMessages: int
      :param debug: Enable debug logging.
      :type debug: bool
      """
      ## lazy import to avoid reactor install upon module import
      if reactor is None:
         from twisted.internet import reactor
      self.reactor = reactor
      self.factory = factory
      Resource.__init__(self)

      self._debug = debug
      self._timeout = timeout
      self._killAfter = killAfter
      self._queueLimitBytes = queueLimitBytes
      self._queueLimitMessages = queueLimitMessages

      if serializers is None:
         serializers = [JsonSerializer()]

      self._serializers = {}
      for ser in serializers:
         self._serializers[ser.SERIALIZER_ID] = ser

      self._transports = {}


      ## <Base URL>/open
      ##
      self.putChild("open", WampHttpResourceOpen(self, allowed_origins))
      self.putChild("close", WampHttpResourceClose(self, allowed_origins))
      if self._debug:
         log.msg("WampHttpResource initialized")


   def getChild(self, name, request):
      """
      Returns send/receive resource for transport.

      <Base URL>/<Transport ID>/send
      <Base URL>/<Transport ID>/receive
      """
      if name not in self._transports:
         return NoResource("No WAMP transport '%s'" % name)

      if len(request.postpath) != 1 or request.postpath[0] not in ['send', 'receive']:
         return NoResource("Invalid WAMP transport operation '%s'" % request.postpath[0])

      return self._transports[name]


   def setStandardHeaders(self, request):
      """
      Set standard HTTP response headers.
      """
      request.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')


