#!/usr/bin/env python

# groupwise-ics: synchronize GroupWise calendar to ICS file and back
# Copyright (C) 2013  Cedric Bosdonnat <cedric@bosdonnat.fr>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import imaplib
import sys
from cal import Calendar
from datetime import datetime
import os.path
import httplib
import xml.etree.ElementTree as ET
import shutil

class GWConnection:
    def __init__(self, server, debug = False):
        self.is_debug = debug
        self.imap = imaplib.IMAP4_SSL(server)
        self.timezones = {}

    def debug(self, message):
        if self.is_debug:
            print >> sys.stderr, 'DEBUG %s\n' % (message)


    def connect(self, login, passwd, mailbox):
        self.imap.login(login, passwd)
        self.imap.select(mailbox)

    def get_mails_ids(self):
        err, ids = self.imap.search(None, '(ALL)')
        return ids[0].split()

    def get_event(self, mail_id, attach_write_func):
        # TODO Caching the events would be needed,
        # though we still need to find a way to get changed appointments
        err, data = self.imap.fetch(mail_id, '(RFC822)')
        self.debug('Mail content to parse: \n------\n%s\n' % data[0][1])
        calendar = Calendar(data[0][1], attach_write_func)
        event = None
        if len(calendar.events) > 0:
            event = calendar.events[0]
        if len(calendar.timezones) > 0:
            for key in calendar.timezones:
                self.timezones[key] = calendar.timezones[key]
        self.debug("%s\n" % calendar.to_ical)
        return event

    def dump(self, path):

        dirname = None
        attachdir_path = os.path.join(os.getcwd(), 'attachments')
        if path is not None:
            dirname = os.path.dirname(path)
            attachdir_path = os.path.join(dirname, 'attachments')
            if not os.path.isdir(dirname):
                os.makedirs(dirname)

        def attach_write_func(name, content):
            attach_path = os.path.join(attachdir_path, name)
            attach_dir = os.path.dirname(attach_path)
            if not os.path.exists(attach_dir):
                os.makedirs(attach_dir)
            if not os.path.exists(attach_path):
                fdescr = open(attach_path, 'w')
                fdescr.write(content)
                fdescr.close()

            return 'file://%s' % attach_path

        # Cleanup existing files
        if (os.path.isdir(attachdir_path)):
            shutil.rmtree(attachdir_path)
        os.makedirs(attachdir_path)

        events = {}
        ids = self.get_mails_ids( );
        for mail_id in ids:
            event = self.get_event(mail_id, attach_write_func)
            if event is None:
                continue
            dtstamp = datetime.strptime(event.dtstamp, '%Y%m%dT%H%M%SZ')
            uid = event.uid

            if uid is not None:
                if uid in events and \
                        datetime.strptime(events[uid].dtstamp, '%Y%m%dT%H%M%SZ') <= dtstamp:
                    events[uid] = event
                elif uid not in events:
                    events[uid] = event

        if path is not None:
            fp = open(path, 'w')
        else:
            fp = sys.stdout

        fp.write('BEGIN:VCALENDAR\r\n')
        fp.write('PRODID:-//SUSE Hackweek//NONSGML groupwise-to-ics//EN\r\n')
        fp.write('VERSION:2.0\r\n')

        for timezone in self.timezones:
            fp.write('\r\n'.join(self.timezones[timezone]))
            fp.write('\r\n')

        for eventid in events:
            event = events[eventid]
            fp.write(event.to_ical())

        fp.write('END:VCALENDAR\r\n')
        if path is not None:
            fp.close()

class SoapException(Exception):
    def __init__(self, msg):
        self.msg = msg
    def __str__(self):
        return self.msg

class GwSoapClient(object):
    def __init__(self, server, port, username, passwd):
        self.server = server
        self.port = port
        self.username = username
        self.passwd = passwd
        self.session = None

        self.http = httplib.HTTPSConnection(server, port)

    def createEnvelope(self, request):

        soap_header = ''
        if self.session is not None:
            soap_header = '<SOAP-ENV:Header><session>%s</session></SOAP-ENV:Header>' % self.session

        header = '''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="http://schemas.novell.com/2005/01/GroupWise/types" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ns2="http://schemas.novell.com/2005/01/GroupWise/methods">
%s
<SOAP-ENV:Body>''' % (soap_header)

        footer = '''</SOAP-ENV:Body></SOAP-ENV:Envelope>'''

        body = '%s%s%s' % (header, request, footer)
        return body

    def request(self, request, body):
        headers = {'SOAPAction': request, \
                   'Content-Type': 'text/xml;charset=utf-8'}
        envelope = self.createEnvelope(body)
        self.http.request('POST', '/soap', envelope, headers)

        response = self.http.getresponse()
        response_body = response.read()
        return response_body

    def connect(self):
        if self.session is not None:
            # Already connected
            return

        login_request = '''
            <ns2:loginRequest>
              <ns2:auth xsi:type="ns1:PlainText">
                <ns1:username>%s</ns1:username>
                <ns1:password>%s</ns1:password>
              </ns2:auth>
            </ns2:loginRequest>''' % (self.username, self.passwd)

        response = self.request('loginRequest', login_request)

        root = ET.fromstring(response)
        ns = {'gwm': 'http://schemas.novell.com/2005/01/GroupWise/methods', \
              'gwt': 'http://schemas.novell.com/2005/01/GroupWise/types'}
        result = root.findall('.//gwm:loginResponse/gwm:session', ns)

        if len(result) == 0:
            raise SoapException('Failed to login')
        self.session = result[0].text

    def logout(self):
        if self.session is None:
            # Not connected, so need to disconnect
            return

        request = '<ns2:logoutRequest>'
        self.request('logoutRequest', request)

    def get_item(self, itemid):
        # autoconnect
        if self.session is None:
            self.connect()

        request = '''
<ns2:getItemRequest>
  <ns2:id>%s</ns2:id>
</ns2:getItemRequest>''' % itemid
        response = self.request('getItemRequest', request)
        print response

    def get_folder_id_by_type(self, folder_type):
        # autoconnect
        if self.session is None:
            self.connect()

        request = '''
<ns2:getFolderRequest>
  <ns2:folderType>%s</ns2:folderType>
</ns2:getFolderRequest>''' % folder_type
        response = self.request('getFolderRequest', request)

        root = ET.fromstring(response)
        ns = {'gwm': 'http://schemas.novell.com/2005/01/GroupWise/methods', \
              'gwt': 'http://schemas.novell.com/2005/01/GroupWise/types'}
        ids = root.findall('.//gwm:getFolderResponse/gwm:folder/gwt:id', ns)

        print ids

        result = None
        if len(ids) > 0:
            result = ids[0].text
        return result


    def get_folder_id(self, parent_id, name):
        # autoconnect
        if self.session is None:
            self.connect()

        if parent_id is None:
            parent_id = 'folders'
        request = '''
<ns2:getFolderListRequest>
  <ns2:parent>%s</ns2:parent>
</ns2:getFolderListRequest>''' % parent_id
        response = self.request('getFolderListRequest', request)

        root = ET.fromstring(response)
        ns = {'gwm': 'http://schemas.novell.com/2005/01/GroupWise/methods', \
              'gwt': 'http://schemas.novell.com/2005/01/GroupWise/types'}
        folders = root.findall('.//gwm:getFolderListResponse/gwm:folders/gwt:folder', ns)

        result = None
        for folder in folders:
            if folder.findall('./gwt:name', ns)[0].text == name:
                result = folder.findall('./gwt:id', ns)[0].text
                break
        return result
