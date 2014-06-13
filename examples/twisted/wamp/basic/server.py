###############################################################################
##
##  Copyright (C) 2011-2014 Tavendo GmbH
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
from autobahn.twisted.wamp import RouterSession
from twisted.internet.defer import Deferred
from autobahn.twisted.websocket import WampWebSocketServerProtocol
import Cookie
from autobahn.util import newid, utcnow

class AuthProtocol(WampWebSocketServerProtocol):

   ## authid -> cookie -> set(connection)
   _authenticated = None
   def onConnect(self, request):
      protocol, headers = WampWebSocketServerProtocol.onConnect(self, request)
      if not hasattr(self.factory,"_cookies"):
         self.factory._cookies = {}
      ## our cookie tracking ID
      self._cbtid = None

      ## see if there already is a cookie set ..
      if request.headers.has_key('cookie'):
         try:
            cookie = Cookie.SimpleCookie()
            cookie.load(str(request.headers['cookie']))
         except Cookie.CookieError:
            pass
         else:
            if cookie.has_key('cbtid'):
               cbtid = cookie['cbtid'].value
               if self.factory._cookies.has_key(cbtid):
                  self._cbtid = cbtid
                  log.msg("Cookie already set: %s" % self._cbtid)

      ## if no cookie is set, create a new one ..
      if self._cbtid is None:

         self._cbtid = newid()
         maxAge = 86400

         cbtData = {'created': utcnow(),
                    'authenticated': None,
                    'maxAge': maxAge,
                    'connections': set()}

         self.factory._cookies[self._cbtid] = cbtData

         ## do NOT add the "secure" cookie attribute! "secure" refers to the
         ## scheme of the Web page that triggered the WS, not WS itself!!
         ##
         headers['Set-Cookie'] = 'cbtid=%s;max-age=%d' % (self._cbtid, maxAge)
         log.msg("Setting new cookie: %s" % self._cbtid)

      ## add this WebSocket connection to the set of connections
      ## associated with the same cookie
      self.factory._cookies[self._cbtid]['connections'].add(self)

      self._authenticated = self.factory._cookies[self._cbtid]['authenticated']

      ## accept the WebSocket connection, speaking subprotocol `protocol`
      ## and setting HTTP headers `headers`
      return (protocol, headers)

class MyRouterSession(RouterSession):

   def onOpen(self, transport):

      RouterSession.onOpen(self, transport)
      print "transport authenticated: {}".format(self._transport._authenticated)


   def onHello(self, realm, details):
      print "onHello: {} {}".format(realm, details)
      if self._transport._authenticated is not None:
         return types.Accept(authid = self._transport._authenticated)
      else:
         return types.Challenge("plain")
      return accept


   def onLeave(self, details):
      if details.reason == "wamp.close.logout":
         cookie = self._transport.factory._cookies[self._transport._cbtid]
         cookie['authenticated'] = None
         for proto in cookie['connections']:
            proto.sendClose()


   def onAuthenticate(self, signature, extra):
      print "onAuthenticate: {} {}".format(signature, extra)

      dres = Deferred()

      ## The client did it's Mozilla Persona authentication thing
      ## and now wants to verify the authentication and login.
      assertion = signature


      log.msg("Authentication request sent.")

      dres.callback(types.Accept(authid = "arno"))
      return dres

if __name__ == '__main__':

   import sys, argparse

   from twisted.python import log
   from twisted.internet.endpoints import serverFromString


   ## parse command line arguments
   ##
   parser = argparse.ArgumentParser()

   parser.add_argument("-d", "--debug", action = "store_true", default = True,
                       help = "Enable debug output.")

   parser.add_argument("-c", "--component", type = str, default = "rpc.timeservice.backend.Component",
                       help = "Start WAMP server with this application component, e.g. 'timeservice.TimeServiceBackend', or None.")

   parser.add_argument("-r", "--realm", type = str, default = "realm1",
                       help = "The WAMP realm to start the component in (if any).")

   parser.add_argument("--endpoint", type = str, default = "tcp:8080",
                       help = 'Twisted server endpoint descriptor, e.g. "tcp:8080" or "unix:/tmp/mywebsocket".')

   parser.add_argument("--transport", choices = ['websocket', 'rawsocket-json', 'rawsocket-msgpack', 'longpoll'], default = "websocket",
                       help = 'WAMP transport type')

   parser.add_argument("-s", "--static-dir", type = str, default="/Users/arno/Documents/PycharmProjects/AutobahnJS-Arno/",
                       help = 'static resources to be served on http, will be available under /static')

   args = parser.parse_args()


   ## start Twisted logging to stdout
   ##
   if args.debug:
      log.startLogging(sys.stdout)


   ## we use an Autobahn utility to install the "best" available Twisted reactor
   ##
   from autobahn.twisted.choosereactor import install_reactor
   reactor = install_reactor()
   if args.debug:
      print("Running on reactor {}".format(reactor))


   ## create a WAMP router factory
   ##
   from autobahn.wamp.router import RouterFactory
   router_factory = RouterFactory()


   ## create a WAMP router session factory
   ##
   from autobahn.twisted.wamp import RouterSessionFactory
   session_factory = RouterSessionFactory(router_factory)
   session_factory.session = RouterSession


   ## if asked to start an embedded application component ..
   ##
   if args.component:
      ## dynamically load the application component ..
      ##
      import importlib
      c = args.component.split('.')
      mod, klass = '.'.join(c[:-1]), c[-1]
      app = importlib.import_module(mod)
      SessionKlass = getattr(app, klass)

      ## .. and create and add an WAMP application session to
      ## run next to the router
      ##
      from autobahn.wamp import types
      session_factory.add(SessionKlass(types.ComponentConfig(realm = args.realm)))


   if args.transport == "websocket":

      ## create a WAMP-over-WebSocket transport server factory with longpoll fallback
      ##
      from autobahn.wamp.serializer import JsonSerializer, MsgPackSerializer

      from autobahn.wamp.http import WampHttpWebsocketServerFactory


      serializers = [MsgPackSerializer(), JsonSerializer()]

      transport_factory = WampHttpWebsocketServerFactory(session_factory, serializers, debug=True, timeout=100,
                                                         killAfter=120, allowed_origins="*")
      #transport_factory.setProtocol(AuthProtocol)
      transport_factory.setProtocolOptions(failByDrop = False)


   elif args.transport in ['rawsocket-json', 'rawsocket-msgpack']:

      ## create a WAMP-over-RawSocket transport server factory
      ##
      if args.transport == 'rawsocket-msgpack':
         from autobahn.wamp.serializer import MsgPackSerializer
         serializer = MsgPackSerializer()
      elif args.transport == 'rawsocket-json':
         from autobahn.wamp.serializer import JsonSerializer
         serializer = JsonSerializer()
      else:
         raise Exception("should not arrive here")

      from autobahn.twisted.rawsocket import WampRawSocketServerFactory
      transport_factory = WampRawSocketServerFactory(session_factory, serializer, debug = args.debug)

   else:
      raise Exception("should not arrive here")

   ## start the server from an endpoint
   ##
   server = serverFromString(reactor, args.endpoint)
   server.listen(transport_factory)




   ## now enter the Twisted reactor loop
   ##
   reactor.run()
