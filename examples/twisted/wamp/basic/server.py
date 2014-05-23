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

   parser.add_argument("-s", "--static-dir", type = str, default="/Users/arno/Documents/PycharmProjects/AutobahnJS/",
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
      from autobahn.twisted.websocket import WampWebSocketServerFactory
      from autobahn.twisted.resource import WebSocketResource
      from twisted.web.server import Site
      from twisted.web.static import File
      from autobahn.wamp.http import WampHttpResource
      ws_factory = WampWebSocketServerFactory(session_factory, debug_wamp = args.debug)
      ws_factory.setProtocolOptions(failByDrop = False)
      serializers = [MsgPackSerializer(), JsonSerializer()]

      resource = WampHttpResource(serializers, debug=True, timeout=100, killAfter=120)
      resource.factory = ws_factory
      root = File("longpoll")

      root.putChild("ws", WebSocketResource(ws_factory))
      root.putChild("longpoll", resource)
      if args.static_dir:
          #root.putChild("web", File("/Users/arno/Documents/PycharmProjects/AutobahnJS/test/"))
          #root.putChild("autobahn.js", File("/Users/arno/Documents/PycharmProjects/AutobahnJS/build/autobahn.js"))
          #root.putChild("lib", File("/Users/arno/Documents/PycharmProjects/AutobahnJS/package/lib/"))
          root.putChild("static", File(args.static_dir))
      transport_factory = Site(root)
      transport_factory.log = lambda _: None # disable any logging


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
