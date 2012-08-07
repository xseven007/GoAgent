#!/usr/bin/env python
# coding=utf-8

__version__ = '1.8.20'
__author__ =  'hkxseven007@gmail.com'
__password__ = ''

import sys, os, re, time, struct, zlib, binascii, logging, httplib, urlparse
try:
    from google.appengine.api import urlfetch
    from google.appengine.runtime import apiproxy_errors, DeadlineExceededError
except ImportError:
    urlfetch = None
try:
    import sae
except ImportError:
    sae = None
try:
    import socket, select, ssl, thread
except:
    socket = None

FetchMax = 3
FetchMaxSize = 1024*1024*4
Deadline = 30

def io_copy(source, dest):
    try:
        io_read  = getattr(source, 'read', None) or getattr(source, 'recv')
        io_write = getattr(dest, 'write', None) or getattr(dest, 'sendall')
        while 1:
            data = io_read(8192)
            if not data:
                break
            io_write(data)
    except Exception as e:
        logging.exception('io_copy(source=%r, dest=%r) error: %s', source, dest, e)
    finally:
        pass

def paas_application(environ, start_response):
    method       = environ['REQUEST_METHOD']
    path_info    = environ['PATH_INFO']
    query_string = environ['QUERY_STRING']
    wsgi_input   = environ['wsgi.input']

    headers = dict((x.replace('HTTP_', '').replace('_', '-').title(), environ[x]) for x in environ if x.startswith('HTTP_'))
    for keyword in ('CONTENT_LENGTH', 'CONTENT_TYPE'):
        if keyword in environ:
            headers[keyword.replace('_', '-').title()] = environ[keyword]
    if 'X-Forwarded-Host' in headers:
        headers['Host'] = headers.pop('X-Forwarded-Host')
    headers['Connection'] = 'close'

    if method == 'CONNECT':
        host, _, port = headers['Host'].rpartition(':')
        port = int(port)
        try:
            logging.info('socket.create_connection((host=%r, port=%r), timeout=%r)', host, port, Deadline)
            sock = socket.create_connection((host, port), timeout=Deadline)
            logging.info('CONNECT %s:%s OK', host, port)
            start_response('201 Tunnel', [])
            if 'gevent.monkey' in sys.modules:
                logging.info('io_copy(wsgi_input.rfile._sock=%r, sock=%r)', wsgi_input.rfile._sock, sock)
                gevent.spawn(io_copy, wsgi_input.rfile._sock.dup(), sock.dup())
            else:
                thread.start_new_thread(io_copy, wsgi_input, sock.dup())
            while 1:
                data = sock.recv(8192)
                if not data:
                    sock.close()
                    raise StopIteration
                yield data
        except socket.error as e:
            raise
    else:
        payload = None
        if 'Content-Length' in headers:
            payload = wsgi_input.read(int(headers.get('Content-Length', -1)))

        url = 'http://%s%s?%s' % (headers['Host'], path_info, query_string)
        scheme, netloc, path, params, query, fragment = urlparse.urlparse(url)
        HTTPConnection = httplib.HTTPSConnection if headers.pop('X-Forwarded-Scheme', 'http') == 'https' else httplib.HTTPConnection
        if params:
            path += ';' + params
        if query:
            path += '?' + query
        try:
            conn = HTTPConnection(netloc, timeout=Deadline)
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            status_line = '%s %s' % (response.status, httplib.responses.get(response.status, 'UNKNOWN'))
            headers = []
            for keyword, value in response.getheaders():
                if keyword == 'connection':
                    headers.append(('Connection', 'close'))
                if keyword != 'set-cookie':
                    headers.append((keyword.title(), value))
                else:
                    scs = value.split(', ')
                    cookies = []
                    i = -1
                    for sc in scs:
                        if re.match(r'[^ =]+ ', sc):
                            try:
                                cookies[i] = '%s, %s' % (cookies[i], sc)
                            except IndexError:
                                pass
                        else:
                            cookies.append(sc)
                            i += 1
                    headers += [('Set-Cookie', x) for x in cookies]
            start_response(status_line, headers)
            while 1:
                data = response.read(8192)
                if not data:
                    response.close()
                    conn.close()
                    raise StopIteration
                else:
                    yield data
        except httplib.HTTPException as e:
            raise

def encode_data(dic):
    return '&'.join('%s=%s' % (k, binascii.b2a_hex(v)) for k, v in dic.iteritems() if v)

def decode_data(qs):
    return dict((k,binascii.a2b_hex(v)) for k, _, v in (x.partition('=') for x in qs.split('&')))

def send_response(start_response, status, headers, content, content_type='image/gif'):
    strheaders = encode_data(headers)
    #logging.debug('response status=%s, headers=%s, content length=%d', status, headers, len(content))
    if headers.get('content-type', '').startswith(('text/', 'application/json', 'application/javascript')):
        data = '1' + zlib.compress('%s%s%s' % (struct.pack('>3I', status, len(strheaders), len(content)), strheaders, content))
    else:
        data = '0%s%s%s' % (struct.pack('>3I', status, len(strheaders), len(content)), strheaders, content)
    start_response('200 OK', [('Content-type', content_type)])
    return [data]

def send_notify(start_response, method, url, status, content):
    logging.warning('%r Failed: url=%r, status=%r', method, url, status)
    content = '<h2>Python Server Fetch Info</h2><hr noshade="noshade"><p>%s %r</p><p>Return Code: %d</p><p>Message: %s</p>' % (method, url, status, content)
    send_response(start_response, status, {'content-type':'text/html'}, content)

def gae_post(environ, start_response):
    request = decode_data(zlib.decompress(environ['wsgi.input'].read(int(environ['CONTENT_LENGTH']))))
    #logging.debug('post() get fetch request %s', request)

    method = request['method']
    url = request['url']
    payload = request['payload']

    if __password__ and __password__ != request.get('password', ''):
        return send_notify(start_response, method, url, 403, 'Wrong password.')

    fetchmethod = getattr(urlfetch, method, '')
    if not fetchmethod:
        return send_notify(start_response, method, url, 501, 'Invalid Method')

    deadline = Deadline

    headers = dict((k.title(),v.lstrip()) for k, _, v in (line.partition(':') for line in request['headers'].splitlines()))
    headers['Connection'] = 'close'

    errors = []
    for i in xrange(FetchMax if 'fetchmax' not in request else int(request['fetchmax'])):
        try:
            response = urlfetch.fetch(url, payload, fetchmethod, headers, False, False, deadline, False)
            break
        except apiproxy_errors.OverQuotaError, e:
            time.sleep(4)
        except DeadlineExceededError, e:
            errors.append(str(e))
            logging.error('DeadlineExceededError(deadline=%s, url=%r)', deadline, url)
            time.sleep(1)
            deadline = Deadline * 2
        except urlfetch.DownloadError, e:
            errors.append(str(e))
            logging.error('DownloadError(deadline=%s, url=%r)', deadline, url)
            time.sleep(1)
            deadline = Deadline * 2
        except urlfetch.InvalidURLError, e:
            return send_notify(start_response, method, url, 501, 'Invalid URL: %s' % e)
        except urlfetch.ResponseTooLargeError, e:
            response = e.response
            logging.error('DownloadError(deadline=%s, url=%r) response(%s)', deadline, url, response and response.headers)
            if response and response.headers.get('content-length'):
                response.status_code = 206
                response.headers['accept-ranges']  = 'bytes'
                response.headers['content-range']  = 'bytes 0-%d/%s' % (len(response.content)-1, response.headers['content-length'])
                response.headers['content-length'] = len(response.content)
                break
            else:
                headers['Range'] = 'bytes=0-%d' % FetchMaxSize
            deadline = Deadline * 2
        except Exception, e:
            errors.append(str(e))
            if i==0 and method=='GET':
                deadline = Deadline * 2
    else:
        return send_notify(start_response, method, url, 500, 'Python Server: Urlfetch error: %s' % errors)

    headers = response.headers
    if 'set-cookie' in headers:
        scs = headers['set-cookie'].split(', ')
        cookies = []
        i = -1
        for sc in scs:
            if re.match(r'[^ =]+ ', sc):
                try:
                    cookies[i] = '%s, %s' % (cookies[i], sc)
                except IndexError:
                    pass
            else:
                cookies.append(sc)
                i += 1
        headers['set-cookie'] = '\r\nSet-Cookie: '.join(cookies)
    headers['connection'] = 'close'
    return send_response(start_response, response.status_code, headers, response.content)

def gae_get(environ, start_response):
    timestamp = long(os.environ['CURRENT_VERSION_ID'].split('.')[1])/pow(2,28)
    ctime = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(timestamp+8*3600))
    html = u'GoAgent Server Xseven Special edition %s \u5df2\u7ecf\u5728\u5de5\u4f5c\u4e86\uff0c\u90e8\u7f72\u65f6\u95f4 %s\n' % (__version__, ctime)
    start_response('200 OK', [('Content-type', 'text/plain; charset=utf-8')])
    return [html.encode('utf8')]

def app(environ, start_response):
    if urlfetch and environ['REQUEST_METHOD'] == 'POST':
        return gae_post(environ, start_response)
    elif not urlfetch:
        return paas_application(environ, start_response)
    else:
        return gae_get(environ, start_response)

if sae:
    application = sae.create_wsgi_app(app)
else:
    application = app

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - - %(asctime)s %(message)s', datefmt='[%b %d %H:%M:%S]')
    import gevent, gevent.pywsgi, gevent.monkey
    gevent.monkey.patch_all(dns=gevent.version_info[0]>=1)
    def read_requestline(self):
        line = self.rfile.readline(8192)
        while line == '\r\n':
            line = self.rfile.readline(8192)
        return line
    gevent.pywsgi.WSGIHandler.read_requestline = read_requestline
    host, _, port = sys.argv[1].rpartition(':') if len(sys.argv) == 2 else ('', ':', 443)
    if '-ssl' in sys.argv[1:]:
        ssl_args = dict(certfile=os.path.splitext(__file__)[0]+'.pem')
    else:
        ssl_args = dict()
    server = gevent.pywsgi.WSGIServer((host, int(port)), application, **ssl_args)
    server.environ.pop('SERVER_SOFTWARE')
    logging.info('serving %s://%s:%s/wsgi.py', 'https' if ssl_args else 'http', server.address[0] or '0.0.0.0', server.address[1])
    server.serve_forever()

