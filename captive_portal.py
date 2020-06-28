#!/usr/bin/python3
import http.server
import subprocess
import cgi
import os
import datetime
import binascii
import re
import threading
import ssl
import urllib
import json
import html
import socket
import dnslib



''' Configuration
-----------------------------------'''

# Server Information
LOCAL_SERVER_IP = "192.168.20.1"
HTTP_SERVER_PORT = 80
HTTPS_SERVER_PORT = 443
REMOTE_SERVER_DOMAIN = "captive.ddns.net"
REMOTE_SERVER_IP = socket.gethostbyname(REMOTE_SERVER_DOMAIN)
# Interfaces
INTERFACE_INPUT = "wlan0"
INTERFACE_OUTPUT = "eth0"
# Files
PAGES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pages')
# iptables
IPTABLES_RESET = True
IPTABLES_FORWARD = True
IPTABLES_INIT = True
# HTTPS
SSL_CERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cert.pem')
SSL_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'key.pem')
# Custom certificate
# openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365

# SSO (Configuration has to be inside the sso_config.py file)
SSO_FACEBOOK_APP_ID = None
SSO_FACEBOOK_APP_SECRET = None
from sso_config import *

# Local DNS Server
USE_CUSTOM_DNS_SERVER = True
LOCAL_DNS_SERVER_IP = LOCAL_SERVER_IP
DNS_SERVER_PORT = 53

# Exclude Facebook addresses
SSO_FACEBOOK_EXCLUDE_DOMAINS = [
    "facebook.com",
    "www.facebook.com",
    "static.xx.fbcdn.net"
]
SSO_FACEBOOK_EXCLUDE_IPS = []
for domain in SSO_FACEBOOK_EXCLUDE_DOMAINS:
    ip = socket.gethostbyname(domain)
    if not (ip in SSO_FACEBOOK_EXCLUDE_IPS):
        SSO_FACEBOOK_EXCLUDE_IPS.append(ip)

# Create remote link
REMOTE_SERVER_LINK = "https://" + REMOTE_SERVER_DOMAIN + ":" + str(HTTPS_SERVER_PORT) + "/"
if str(HTTPS_SERVER_PORT) == "443":
    REMOTE_SERVER_LINK = "https://" + REMOTE_SERVER_DOMAIN + "/"

# Authorizations Daemon
AUTHDAEMON_INTERVAL_CHECK = 10

# Access Times
ACCESS_TIME_INTERNET = 2*60*60
ACCESS_TIME_FACEBOOK_LOGIN = 2*60


''' Authorizations Monitor Daemon
-----------------------------------'''
authDaemon = None
class AuthorizationsDaemon:
    def __init__(self):
        self.authorizations = {}
        self.clients = {}
        self.sessions = []
        self.ip_sessions = {}

    def runChecks(self):
        self.checkExpiredSessions()
        self.checkMacBindings()

    def checkExpiredSessions(self):
        now = datetime.datetime.now()
        expired = []
        for session in self.sessions:
            if session["expiration"] < now:
                expired.append(session)
        # Revoke authorization on expired session
        self.deauthorizeSessions(expired)

    def checkMacBindings(self):
        now = datetime.datetime.now()
        clients = getArpList()
        for client in clients:
            ip = client["ip"]
            mac = client["mac"]
            # If client was previously logged
            if ip in self.clients.keys() and self.clients[ip]["mac"] != None:
                # Check if MAC matches previous MAC
                if self.clients[ip]["mac"] != mac:
                    self.log("MAC change detected on " + ip + " : " + self.clients[ip]["mac"] + " --> " + mac)
                    # De-authorize client
                    self.clients[ip]["mac"] = None
                    self.clients[ip]["logged"] = now
                    self.deauthorizeIP_All(ip);
            # Log user
            else:
                self.clients[ip] = {
                    "mac" : mac,
                    "logged" : now
                }

    def prepare_session(self, ip, stype, expiration):
        session = {
            "ip" : ip,
            "mac" : getMacFromIp(ip),
            "type" : stype,
            "expiration" : expiration
        }
        return session

    # Update Authorizations
    def reauthorizeSession(self, session, seconds):
        self.log("Update " + session["ip"] + " to " + session["type"])
        session["expiration"] = datetime.datetime.now() + datetime.timedelta(seconds=seconds)

    def reauthorizeSessions(self, sessions, seconds):
        for session in sessions:
            self.reauthorizeSession(session, seconds)


    # Authorizations
    def authorizeSession(self, session):
        self.log("Authorize " + session["ip"] + " to " + session["type"])
        self.sessions.append(session)
        ip = session["ip"]
        if not (ip in self.ip_sessions.keys()):
            self.ip_sessions[ip] = []
        self.ip_sessions[ip].append(session)
        # Allow access to Internet
        if session["type"] == "Internet":
            # The nat rule has to be inserted under the captive's portal domain
            callCmd(["iptables", "-t", "nat", "-I", "PREROUTING", "2", "-s", ip, "-j" ,"ACCEPT"])
            callCmd(["iptables",              "-I",    "FORWARD", "1", "-s", ip, "-j" ,"ACCEPT"])
        # Allow access to Facebook
        elif session["type"] == "Facebook-Login":
            # Allow Facebook IPs
            for ip_addresses in SSO_FACEBOOK_EXCLUDE_IPS:
                callCmd(["iptables", "-I", "FORWARD", "-i", INTERFACE_INPUT, "-p", "tcp", "-s", ip, "-d", ip_addresses, "--dport", str(443), "-j" , "ACCEPT"])
        # Update client info
        self.setClientAuthorizations(ip, session["type"], True)

    def authorizeSessions(self, sessions):
        for session in sessions:
            self.authorizeSession(self, session)

    def authorizeIP_Internet(self, ip, seconds):
        sessions = self.getSessionsByIP(ip, "Internet")
        if len(sessions) > 0:
            self.reauthorizeSessions(sessions, seconds)
        else:
            session = self.prepare_session(ip, "Internet", datetime.datetime.now() + datetime.timedelta(seconds=seconds))
            self.authorizeSession(session)

    def authorizeIP_FacebookLogin(self, ip, seconds):
        sessions = self.getSessionsByIP(ip, "Facebook-Login")
        if len(sessions) > 0:
            self.reauthorizeSessions(sessions, seconds)
        else:
            session = self.prepare_session(ip, "Facebook-Login", datetime.datetime.now() + datetime.timedelta(seconds=seconds))
            self.authorizeSession(session)


    # De-authorizations
    def deauthorizeSession(self, session):
        self.log("De-authorize " + session["ip"] + " from " + session["type"])
        self.sessions.remove(session)
        ip = session["ip"]
        if ip in self.ip_sessions.keys():
            self.ip_sessions[ip].remove(session)
        # Block access to Internet
        if session["type"] == "Internet":
            callCmd(["iptables", "-t", "nat", "-D", "PREROUTING", "-s", ip, "-j" ,"ACCEPT"])
            callCmd(["iptables",              "-D",    "FORWARD", "-s", ip, "-j" ,"ACCEPT"])
        # Block access to Facebook
        elif session["type"] == "Facebook-Login":
            # Allow Facebook IPs
            for ip_addresses in SSO_FACEBOOK_EXCLUDE_IPS:
                callCmd(["iptables", "-D", "FORWARD", "-i", INTERFACE_INPUT, "-p", "tcp", "-s", ip, "-d", ip_addresses, "--dport", str(443), "-j" , "ACCEPT"])
        # Update client info
        self.setClientAuthorizations(ip, session["type"], False)

    def deauthorizeSessions(self, sessions):
        for session in sessions:
            self.deauthorizeSession(session)

    def deauthorizeIP_Internet(self, ip):
        session = self.getSessionsByIP(ip, "Internet")
        self.deauthorizeSessions(session)

    def deauthorizeIP_FacebookLogin(self, ip):
        session = self.getSessionsByIP(ip, "Facebook-Login")
        self.deauthorizeSessions(session)

    def deauthorizeIP_All(self, ip):
        session = self.getSessionsByIP(ip)
        self.deauthorizeSessions(session)


    # Client info
    def getClientAuthorizations(self, ip):
        if not (ip in self.authorizations.keys()):
            self.authorizations[ip] = {
                "Internet" : False,
                "Facebook-Login" : False
            }
        return self.authorizations[ip]

    def setClientAuthorizations(self, ip, stype, value):
        self.getClientAuthorizations(ip)
        self.authorizations[ip][stype] = value
    
    def hasClientAuthorization(self, ip, stype):
        info = self.getClientAuthorizations(ip);
        if stype in self.authorizations[ip].keys():
            return self.authorizations[ip][stype]
        return False

    def hasClient_Internet(self, ip):
        return self.hasClientAuthorization(ip, "Internet")


    # Other function
    def getSessionsByIP(self, ip, stype=None):
        sessions = []
        if ip in self.ip_sessions.keys():
            for session in self.ip_sessions[ip]:
                if stype == None or stype == session["type"]:
                    sessions.append(session)
        return sessions

    def log(self, message):
        print("[AuthDaemon] " + message)


            


''' HTTPS Captive Portal (Main Captive Portal)
-----------------------------------'''

# This it the HTTP server used by the the captive portal
class CaptivePortal(http.server.BaseHTTPRequestHandler):

    server_variables = {
        "server_ip" : LOCAL_SERVER_IP,
        "server_port" : HTTPS_SERVER_PORT,
        "year" : datetime.datetime.now().year
    }

    sessions = {}

    route = {
        #"/index": {"file": "index.html", "cached": False},
        "/login": {"file": "login.html", "cached": False},
        "/status": {"file": "status.html", "cached": False},
        "/favicon.ico": {"file": "favicon.ico", "cached": False},
        "/css/custom.css": {"file": "css/custom.css", "cached": False},
        "/css/bootstrap.min.css": {"file": "css/bootstrap.min.css", "cached": False},
        "/css/bootstrap.lumen.min.css": {"file": "css/bootstrap.lumen.min.css", "cached": False},
        "/js/jquery.min.js": {"file": "js/jquery.min.js", "cached": False},
        "/js/popper.min.js": {"file": "js/popper.min.js", "cached": False},
        "/js/bootstrap.min.js": {"file": "js/bootstrap.min.js", "cached": False},
        "/img/portal.png": {"file": "img/portal.png", "cached": False},
        "/img/portal-other.png": {"file": "img/portal-other.png", "cached": False},

        # Other pages
        ".redirect": {"file": "redirect.html", "cached": False},
        ".message": {"file": "message.html", "cached": False},
    }

    route_alias = {
        "/": "/login"
    }

    def get_route(self, rawUrl):
        # Analise URL
        url = urllib.parse.urlparse(rawUrl)
        parms = urllib.parse.parse_qs(url.query)
        path = url.path
        # Check alias
        if path in self.route_alias.keys():
            path = self.route_alias[path]
        # Get file
        data = self.get_file(path);
        # Headers
        headers = {}
        # Status
        status = 200

        # Print info
        #print("url : " + rawUrl)
        #print("path : " + path)

        # Login Page
        if path == '/login':
            # Check if logged in
            loggedin = self.get_logged_in()
            if loggedin == "Facebook":
                data, headers, status = self.do_redirect("/status", "<p>Redirecting...</p>")
            else:
                data = self.replace_keys_decode(data, {
                    "facebook-link" : "/facebook/init"
                })
        # Logout page
        if path == '/logout':
            self.set_logged_out()
            data, headers, status = self.do_redirect("/", "<p>Logging out...</p>", 5)
        # Status page
        elif path == '/status':
            info = getRuleFromIp(self._session["ip"])
            if info == None:
                info = {"packets" : 0, "bytes" : 0}
            # Check if logged in
            loggedin = self.get_logged_in()
            if loggedin == "Facebook":
                data = self.replace_keys_decode(data, {
                    "title" : "Connected",
                    "name" : html.escape(self.facebook_get_user_name()),
                    "login-type" : "Facebook Login",
                    "packets" : format(info["packets"],',d'),
                    "bytes" : bytes_sizeof_format(info["bytes"]),
                    "refresh-link" : "/status",
                    "logout-link" : "/logout"
                })
            else:
                data, headers, status = self.do_redirect("/login", "<p>Redirecting...</p>")

        # Facebook - Pre-Oauth
        elif path == '/facebook/init':
            fb_redirect = self.facebook_pre_oauth()
            data, headers, status = self.do_redirect(fb_redirect, "<p>You have %d seconds to sign in...</p>" % ACCESS_TIME_FACEBOOK_LOGIN, 5)
        # Facebook - Post-Oauth
        elif path == '/facebook/oauth':
            fb_authcode = ''
            fb_state = ''
            if ('code' in parms.keys()) and ('state' in parms.keys()):
                fb_authcode = parms['code'][0]
                fb_state = parms['state'][0]
            error = self.facebook_post_oauth(fb_authcode, fb_state)
            if error == None:
                self.authorize_internet()
                data, headers, status = self.do_redirect("/status", "<p>Redirecting...</p>")
            else:
                data, headers, status = self.do_message("Failed", "<p>Failed to login with Facebook</p><p><small>Error: %s</small></p>" % html.escape(error))

        return data, headers, status;

    def get_logged_in(self):
        if self.session_hasInternet():
            date = self.session_get("authorized", datetime.datetime(1970, 1, 1))
            if date > datetime.datetime.now():
                date = self.session_get("fb-authorized", datetime.datetime(1970, 1, 1))
                if date > datetime.datetime.now():
                    fb_user_info = self.session_get("fb-user-info", None)
                    if (fb_user_info != None) and ("name" in fb_user_info.keys()):
                        return "Facebook"
        return None

    def set_logged_out(self):
        self.deauthorize_internet()
        self.facebook_deoauth()

    def facebook_deoauth(self):
        self.session_set("fb-access-token", None)
        self.session_set("fb-user-info", None)
        self.session_set("fb-state", None)
        self.session_set("fb-authorized", datetime.datetime(1970, 1, 1))

    def facebook_pre_oauth(self):
        self.facebook_deoauth()
        authDaemon.authorizeIP_FacebookLogin(self._session["ip"], ACCESS_TIME_FACEBOOK_LOGIN)
        fb_state = binascii.b2a_hex(os.urandom(32)).decode("utf-8")
        self.session_set("fb-state", fb_state)
        return "https://www.facebook.com/v7.0/dialog/oauth?client_id=%s&redirect_uri=%s&state=%s" % (SSO_FACEBOOK_APP_ID, REMOTE_SERVER_LINK + "facebook/oauth", fb_state)

    def facebook_post_oauth(self, fb_authcode, fb_state):
        authDaemon.deauthorizeIP_FacebookLogin(self._session["ip"])
        # Check state
        if not (fb_state == self.session_get("fb-state", None)):
            return "Invalid oauth state."
        # Get Facebook access token
        #print("https://graph.facebook.com" + ("/v7.0/oauth/access_token?client_id=%s&redirect_uri=%s&client_secret=%s&code=%s" % (SSO_FACEBOOK_APP_ID, REMOTE_SERVER_LINK + "facebook/oauth", SSO_FACEBOOK_APP_SECRET, fb_authcode)))
        conn = http.client.HTTPSConnection("graph.facebook.com")
        conn.request("GET", "/v7.0/oauth/access_token?client_id=%s&redirect_uri=%s&client_secret=%s&code=%s" % (SSO_FACEBOOK_APP_ID, REMOTE_SERVER_LINK + "facebook/oauth", SSO_FACEBOOK_APP_SECRET, fb_authcode))
        res = conn.getresponse()
        #print(type(res.status), res.status)
        #print(type(res.reason), res.reason)
        #if res.status != 200 or res.reason != "OK":
        #    return "Invalid status was returned (%s,%s)." % (str(res.status), res.reason)
        response = res.read()
        conn.close()
        # Parse response
        fb_access_token = json.loads(response)
        if not ("access_token" in fb_access_token.keys()):
            return "Failed to get access token."
        fb_access_token = fb_access_token["access_token"]
        # Get user info
        conn = http.client.HTTPSConnection("graph.facebook.com")
        conn.request("GET", "/v7.0/me?fields=id,name,email&access_token=%s" % (fb_access_token))
        res = conn.getresponse()
        #if res.status != 200 or res.reason != "OK":
        #    return "Invalid status was returned (%s,%s)." % (str(res.status), res.reason)
        response = res.read()
        conn.close()
        fb_user_info = json.loads(response)
        if not ("id" in fb_user_info.keys() and "name" in fb_user_info.keys()):
            return "Failed to get user info."
        # Save session data
        self.session_set("fb-access-token", fb_access_token)
        self.session_set("fb-user-info", fb_user_info)
        self.session_set("fb-state", None)
        self.session_set("fb-authorized", datetime.datetime.now() + datetime.timedelta(seconds=ACCESS_TIME_INTERNET))
        return None

    def facebook_get_user_id(self):
        return self.session_get("fb-user-info", {"id":0})["id"]

    def facebook_get_user_name(self):
        return self.session_get("fb-user-info", {"name":"Unknown"})["name"]
        

    def get_file(self, name):
        # If route exists
        if name in self.route.keys():
            # If not cached
            if self.route[name]["cached"] == False:
                self.route[name]["cached"] = self.load_file(self.route[name]["file"])
            # Return file
            return self.route[name]["cached"]
        # File not found
        return None

    def load_file(self, path):
        # Calculate path
        path = os.path.join(PAGES_PATH, path)
        # Load file
        file = open(path, "rb")
        data = file.read()
        file.close()
        # If HTML
        name, ext = os.path.splitext(path)
        if ext == ".html":
            data = self.replace_keys_decode(data, self.server_variables)
        # Return file
        return data

    def replace_keys(self, html, variables):
        for name, value in variables.items():
            html = html.replace("{{" + name + "}}", str(value))
        return html

    def replace_keys_decode(self, data, variables):
        return self.replace_keys(data.decode("utf-8"), variables).encode()

    def get_content_type(self, ext):
        # Common files
        if ext == ".css" :
            return "text/css"
        elif ext == ".css" :
            return "text/css"
        elif ext == ".html" :
            return "text/html"
        elif ext == ".js" :
            return "text/javascript"
        elif ext == ".png" :
            return "image/png"
        elif ext == ".jpg" or ext == ".jpeg" :
            return "image/jpeg"
        elif ext == ".svg" :
            return "image/svg+xml"
        elif ext == ".ico" :
            return "image/x-icon"
        return "text/html"

    def session_init(self):
        ip = self.client_address[0]
        #mac = getMacFromIp(ip)
        self._session = {
            "ip" : ip,
            #"mac" : mac
        }
        if not (ip in self.sessions.keys()):
            self.sessions[ip] = {
                "ip" : ip,
                #"mac" : mac,
                "data" : {}
            }
        return

    def session_hasInternet(self):
        if authDaemon.hasClient_Internet(self._session["ip"]) == False:
            return False
        return True

    def session_set(self, key, value):
        self.sessions[self._session["ip"]]["data"][key] = value

    def session_get(self, key, defvalue):
        if key in self.sessions[self._session["ip"]]["data"].keys():
            return self.sessions[self._session["ip"]]["data"][key]
        else:
            return defvalue

    def authorize_internet(self):
        ip = self._session["ip"]
        self.session_set("authorized", datetime.datetime.now() + datetime.timedelta(seconds=ACCESS_TIME_INTERNET))
        authDaemon.authorizeIP_Internet(self._session["ip"], ACCESS_TIME_INTERNET)

    def deauthorize_internet(self):
        ip = self._session["ip"]
        self.session_set("authorized", datetime.datetime(1970, 1, 1))
        authDaemon.deauthorizeIP_All(self._session["ip"])
    
    # Handle GET requests
    def do_GET(self):
        self.session_init()
        # Get file
        body, headers, status = self.get_route(self.path)
        if body == None :
            self.send_response(404)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(str("404: file not found").encode())
            return
        # Path info
        file_name, file_extension = os.path.splitext(self.path)
        # Create headers
        self.send_response(status)
        self.send_header("Content-type", self.get_content_type(file_extension))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        # Return file
        self.wfile.write(body)

    # Handle POST requests
    def do_POST(self):
        # To do
        pass

    def do_redirect(self, location, message, seconds = 0):
        #status = 302
        status = 200
        headers = {"Location": location}
        data = self.get_file(".redirect");
        data = self.replace_keys_decode(data, {
            "location" : location,
            "message" : message,
            "seconds" : str(seconds)
        })
        return data, headers, status;

    def do_message(self, title, message):
        status = 200
        headers = {}
        data = self.get_file(".message");
        data = self.replace_keys_decode(data, {
            "title" : title,
            "message" : message
        })
        return data, headers, status;

    #the following function makes server produce no output
    #comment it out if you want to print diagnostic messages
    def log_message(self, format, *args):
        return



''' HTTP Captive Portal
-----------------------------------'''

#class RedirectPortal(http.server.BaseHTTPRequestHandler):
class RedirectPortal(CaptivePortal):
    route = {
        "/favicon.ico": {"file": "favicon.ico", "cached": False},
        "/css/custom.css": {"file": "css/custom.css", "cached": False},
        "/css/bootstrap.min.css": {"file": "css/bootstrap.min.css", "cached": False},
        "/css/bootstrap.lumen.min.css": {"file": "css/bootstrap.lumen.min.css", "cached": False},
        "/js/jquery.min.js": {"file": "js/jquery.min.js", "cached": False},
        "/js/popper.min.js": {"file": "js/popper.min.js", "cached": False},
        "/js/bootstrap.min.js": {"file": "js/bootstrap.min.js", "cached": False},
        "/img/portal.png": {"file": "img/portal.png", "cached": False},
        "/img/portal-other.png": {"file": "img/portal-other.png", "cached": False},

        # Other pages
        ".redirect": {"file": "redirect.html", "cached": False},
        ".message": {"file": "message.html", "cached": False},
    }

    def get_route(self, rawUrl):
        # Analise URL
        url = urllib.parse.urlparse(rawUrl)
        path = url.path
        # Headers
        headers = {}
        # Status
        status = 200

        # Get file
        data = self.get_file(path);

        # If file not found
        if data == None:
            data, headers, status = self.do_redirect(REMOTE_SERVER_LINK, "<p>Redirecting to captive portal...</p>", 2)

        return data, headers, status;

    # Handle GET requests
    def do_GET(self):
        # Get file
        body, headers, status = self.get_route(self.path)
        if body == None :
            self.send_response(404)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(str("404: file not found").encode())
            return
        # Path info
        file_name, file_extension = os.path.splitext(self.path)
        # Create headers
        self.send_response(status)
        self.send_header("Content-type", self.get_content_type(file_extension))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        # Return file
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()



''' Other Functions
-----------------------------------'''

# Run command
def callCmd(cmd):
    subprocess.call(cmd)

def runCmd(cmd):
    return subprocess.run(cmd, shell=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

# List ARP information
def getArpList():
    # Get arp
    result = runCmd('arp -a')
    if result.returncode != 0:
        return []
    # Parse data
    data = result.stdout.decode('utf-8')
    data = re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9A-Za-z]+:[0-9A-Za-z]+:[0-9A-Za-z]+:[0-9A-Za-z]+:[0-9A-Za-z]+:[0-9A-Za-z]+)\s+\[([^\]]*)\]", data)
    devices = []
    for device in data:
        devices.append({
            'ip' : device[0],
            'mac' : device[1],
            'interface' : device[2]
        })
    # Return data
    return devices

# Get MAC from IP
def getMacFromIp(ip):
    devices = getArpList()
    for device in devices:
        if device['ip'] == ip:
            return device['mac']
    return '00:00:00:00:00:00'

# List rules information
def getRulesList():
    # Get rules
    result = runCmd('iptables -L FORWARD -n -v -x')
    if result.returncode != 0:
        return []
    # Parse data
    # 7609  2108649 ACCEPT     all  --  *      *       192.168.20.97        0.0.0.0/0
    data = result.stdout.decode('utf-8')
    data = re.findall(r"\s+(\d+)\s+(\d+)\s+ACCEPT\s+all\s+--\s+\*\s+\*\s+(\d+\.\d+\.\d+\.\d+)\s+0\.0\.0\.0\/0", data)
    rules = []
    for rule in data:
        rules.append({
            'packets' : int(rule[0]),
            'bytes' : int(rule[1]),
            'ip' : rule[2]
        })
    # Return data
    return rules

# Get Rule from IP
def getRuleFromIp(ip):
    rules = getRulesList()
    for rule in rules:
        if rule['ip'] == ip:
            return rule
    return None

def bytes_sizeof_format(num, suffix='B'):
    for unit in ['','K','M','G','T','P','E','Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Y', suffix)



''' Script Start Functions
-----------------------------------'''

# Start Server
def start_server():
    threading.Thread(target = server_http).start()
    threading.Thread(target = server_https).start()

def server_http():
    print("[webserver] Start HTTP")
    server = http.server.ThreadingHTTPServer(('', HTTP_SERVER_PORT), RedirectPortal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()

def server_https():
    print("[webserver] Start HTTPS")
    #server = http.server.HTTPServer(('', 443), CaptivePortal)
    #server = http.server.ThreadingHTTPServer(('', 443), CaptivePortal)
    server = http.server.ThreadingHTTPServer(('', HTTPS_SERVER_PORT), CaptivePortal)
    server.socket = ssl.wrap_socket(server.socket, keyfile=SSL_KEY_PATH, certfile=SSL_CERT_PATH, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()

def iptables_reset():
    if IPTABLES_RESET == True:
        print("[iptables] Reset")
        callCmd(["iptables", "-P", "INPUT", "ACCEPT"])
        callCmd(["iptables", "-P", "FORWARD", "ACCEPT"])
        callCmd(["iptables", "-P", "OUTPUT", "ACCEPT"])
        callCmd(["iptables", "-t", "nat", "-F"])
        callCmd(["iptables", "-t", "mangle", "-F"])
        callCmd(["iptables", "-F"])
        callCmd(["iptables", "-X"])
    if IPTABLES_FORWARD == True:
        callCmd(["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", INTERFACE_OUTPUT, "-j", "MASQUERADE"])

def iptables_init():
    if IPTABLES_INIT == True:
        print("[iptables] Initialize")
        # Allow DNS
        if not USE_CUSTOM_DNS_SERVER:
            callCmd(["iptables", "-A", "FORWARD", "-i", INTERFACE_INPUT, "-p", "tcp", "--dport", "53", "-j" , "ACCEPT"])
            callCmd(["iptables", "-A", "FORWARD", "-i", INTERFACE_INPUT, "-p", "udp", "--dport", "53", "-j" , "ACCEPT"])
        # Allow traffic to captive portal
        callCmd(["iptables", "-A", "FORWARD", "-i", INTERFACE_INPUT, "-p", "tcp", "-d", LOCAL_SERVER_IP, "--dport", str( HTTP_SERVER_PORT), "-j", "ACCEPT"])
        callCmd(["iptables", "-A", "FORWARD", "-i", INTERFACE_INPUT, "-p", "tcp", "-d", LOCAL_SERVER_IP, "--dport", str(HTTPS_SERVER_PORT), "-j", "ACCEPT"])
        # Block all other traffic
        callCmd(["iptables", "-A", "FORWARD", "-i", INTERFACE_INPUT, "-j" , "DROP"])
        # Redirecting HTTPS traffic to captive portal (traffic towards the domain)
        callCmd(["iptables", "-t", "nat", "-A",  "PREROUTING", "-i", INTERFACE_INPUT, "-p", "tcp", "-d", REMOTE_SERVER_IP, "--dport", str(HTTPS_SERVER_PORT), "-j", "DNAT", "--to-destination",  LOCAL_SERVER_IP + ":" + str(HTTPS_SERVER_PORT)])
        callCmd(["iptables", "-t", "nat", "-A", "POSTROUTING"                       , "-p", "tcp", "-d", LOCAL_SERVER_IP,  "--dport", str(HTTPS_SERVER_PORT), "-j", "SNAT",      "--to-source", REMOTE_SERVER_IP])
        # Redirecting HTTP traffic to captive portal (all HTTP traffic)
        callCmd(["iptables", "-t", "nat", "-A",  "PREROUTING", "-i", INTERFACE_INPUT, "-p", "tcp",                         "--dport", str( HTTP_SERVER_PORT), "-j", "DNAT", "--to-destination",  LOCAL_SERVER_IP + ":" + str( HTTP_SERVER_PORT)])
        # Forward DNS traffic to local DNS
        if USE_CUSTOM_DNS_SERVER:
            callCmd(["iptables", "-t", "nat", "-A",  "PREROUTING", "-i", INTERFACE_INPUT, "-p", "tcp", "--dport", str(53), "-j", "DNAT", "--to-destination",  LOCAL_DNS_SERVER_IP + ":" + str(DNS_SERVER_PORT)])
            callCmd(["iptables", "-t", "nat", "-A",  "PREROUTING", "-i", INTERFACE_INPUT, "-p", "udp", "--dport", str(53), "-j", "DNAT", "--to-destination",  LOCAL_DNS_SERVER_IP + ":" + str(DNS_SERVER_PORT)])

# Start Monitor Daemon
def start_auth_daemon():
    global authDaemon
    print("[AuthDaemon] Start Authorizations Daemon")
    authDaemon = AuthorizationsDaemon()
    auth_daemon_interval()

def auth_daemon_interval():
    threading.Timer(AUTHDAEMON_INTERVAL_CHECK, auth_daemon_interval).start()
    authDaemon.runChecks()



''' Script Start
-----------------------------------'''
if __name__ == '__main__':
    # Check if root
    if os.getuid() != 0:
        print("Need to run with root rights.")
    else:
        # Set up iptables
        iptables_reset()
        iptables_init()
        # Monitor Daemon
        start_auth_daemon()
        # Start Server
        start_server()
