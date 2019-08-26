""" Classes for interacting with Salesforce Bulk API """
import logging
import os
from tempfile import NamedTemporaryFile

import bigjson

try:
    from collections import OrderedDict
except ImportError:
    # Python < 2.7
    from ordereddict import OrderedDict

import json
from time import sleep

import requests

from simple_salesforce.util import exception_handler

import sys


if sys.version[0] == '2':
    string_types = basestring
else:
    string_types = str


class Object:

    _GET_METHOD_RAISE_EXCEPTION = 0
    _GET_METHOD_RETURN_DEFAULT = 1
    _GET_METHOD_RETURN_BOOLEAN = 2

    def __init__(self, reader, read_all):
        self.reader = reader
        self.begin_pos = self.reader._tell_read_pos()
        self.length = -1

        if not read_all:
            return

        self._read_all()

    def keys(self):
        return [keyvalue[0] for keyvalue in self.iteritems()]

    def values(self):
        return [keyvalue[1] for keyvalue in self.iteritems()]

    def items(self):
        return [keyvalue for keyvalue in self.iteritems()]

    def iteritems(self):
        self.reader._seek(self.begin_pos)

        if not self.reader._skip_if_next('{'):
            raise Exception(u'Missing "{"!')

        self.reader._skip_whitespace()

        if self.reader._skip_if_next('}'):
            return

        while True:
            # Read key. Reading all is not required, because strings
            # are read fully anyway, and if it is not string, then
            # there is an error and reading can be canceled.
            key = self.reader.read(read_all=False)
            if not isinstance(key, string_types):
                raise Exception(u'Invalid key type in JSON object!')

            # Skip colon and whitespace around it
            self.reader._skip_whitespace()
            if not self.reader._skip_if_next(':'):
                raise Exception(u'Missing ":"!')
            self.reader._skip_whitespace()

            # Read value
            value = self.reader.read(read_all=True)

            yield (key, value)

            # Read comma or "}" and whitespace around it.
            self.reader._skip_whitespace()
            if self.reader._skip_if_next(','):
                self.reader._skip_whitespace()
            elif self.reader._skip_if_next('}'):
                break
            else:
                raise Exception(u'Expected "," or "}"!')

    def get(self, key, default=None):
        return self._get(key, default, Object._GET_METHOD_RETURN_DEFAULT)

    def to_python(self):
        self.reader._seek(self.begin_pos)
        return self._read_all(to_python=True)

    def _read_all(self, to_python=False):
        """ Reads and validates all bytes in
        the Object. Also counts its length.
        If 'to_python' is set to true, then returns dict.
        """
        if to_python:
            python_dict = {}
        else:
            python_dict = None

        self.length = 0

        self.reader._seek(self.begin_pos)

        if not self.reader._skip_if_next('{'):
            raise Exception(u'Missing "{"!')

        self.reader._skip_whitespace()

        if self.reader._skip_if_next('}'):
            return python_dict

        while True:
            # Skip key. Reading all is not required, because strings
            # are read fully anyway, and if it is not string, then
            # there is an error and reading can be canceled.
            key = self.reader.read(read_all=False)
            if not isinstance(key, string_types):
                raise Exception(u'Invalid key type in JSON object!')

            # Skip colon and whitespace around it
            self.reader._skip_whitespace()
            if not self.reader._skip_if_next(':'):
                raise Exception(u'Missing ":"!')
            self.reader._skip_whitespace()

            # Skip or read value
            if to_python:
                python_dict[key] = self.reader.read(read_all=True, to_python=True)
            else:
                self.reader.read(read_all=True)

            self.length += 1

            # Read comma or "}" and whitespace around it.
            self.reader._skip_whitespace()
            if self.reader._skip_if_next(','):
                self.reader._skip_whitespace()
            elif self.reader._skip_if_next('}'):
                break
            else:
                raise Exception(u'Expected "," or "}"!')

        return python_dict

    def __contains__(self, key):
        return self._get(key, None, Object._GET_METHOD_RETURN_BOOLEAN)

    def __getitem__(self, key):
        return self._get(key, None, Object._GET_METHOD_RAISE_EXCEPTION)

    def _get(self, key, default, method):

        if not isinstance(key, string_types):
            raise TypeError(u'Key must be string!')

        # TODO: Use some kind of lookup table!

        # Rewind to requested element from the beginning
        self.reader._seek(self.begin_pos)
        if not self.reader._skip_if_next('{'):
            raise Exception(u'Missing "{"!')
        self.reader._skip_whitespace()

        if self.reader._is_next('}'):
            if method == Object._GET_METHOD_RAISE_EXCEPTION:
                raise KeyError(key)
            elif method == Object._GET_METHOD_RETURN_DEFAULT:
                return None
            else:
                return False

        while True:
            key2 = self.reader.read(read_all=False)
            if not isinstance(key2, string_types):
                raise Exception(u'Invalid key type in JSON object!')

            # Read colon
            self.reader._skip_whitespace()
            if not self.reader._skip_if_next(':'):
                raise Exception(u'Missing ":"!')
            self.reader._skip_whitespace()

            # If this is the requested value, then it doesn't
            # need to be read fully. If not, then its bytes
            # should be skipped, and it needs to be fully read.
            if key2 == key:
                if method == Object._GET_METHOD_RETURN_BOOLEAN:
                    return True
                else:
                    return self.reader.read(read_all=False)
            else:
                self.reader.read(read_all=True)

            # Skip comma and whitespace around it
            self.reader._skip_whitespace()
            if self.reader._skip_if_next(','):
                self.reader._skip_whitespace()
            elif self.reader._is_next('}'):
                if method == Object._GET_METHOD_RAISE_EXCEPTION:
                    raise KeyError(key)
                elif method == Object._GET_METHOD_RETURN_DEFAULT:
                    return None
                else:
                    return False
            else:
                raise Exception(u'Expected "," or "}"!')

    def __len__(self):
        if self.length < 0:
            self._read_all()
        return self.length

class Array:

    _MAX_INDEX_LOOKUP_LENGTH = 1000

    def __init__(self, reader, read_all):
        self.reader = reader
        self.begin_pos = self.reader._tell_read_pos()
        self.length = -1

        # For optimizing index queries
        self.last_known_pos = 0
        self.last_known_pos_index = 0
        self.index_lookup = []
        self.index_lookup_multiplier = 1

        if not read_all:
            return

        self._read_all()

    def to_python(self):
        self.reader._seek(self.begin_pos)
        return self._read_all(to_python=True)

    def _read_all(self, to_python=False):
        """ Reads and validates all bytes in
        the Array. Also counts its length.
        If 'to_python' is set to true, then returns list.
        """
        if to_python:
            python_list = []
        else:
            python_list = None

        self.length = 0

        self.reader._seek(self.begin_pos)

        if not self.reader._skip_if_next('['):
            raise Exception(u'Missing "["!')

        self.reader._skip_whitespace()

        if self.reader._skip_if_next(']'):
            return python_list

        while True:
            # Skip or read element
            if to_python:
                python_list.append(self.reader.read(read_all=True, to_python=True))
            else:
                self.reader.read(read_all=True)

            self.length += 1

            # Skip comma or "]" and whitespace around it
            self.reader._skip_whitespace()
            if self.reader._skip_if_next(','):
                self.reader._skip_whitespace()
            elif self.reader._skip_if_next(']'):
                break
            else:
                raise Exception(u'Expected "," or "]"!')

        return python_list

    def __getitem__(self, index):
        # TODO: Support negative indexes!

        # Find best known position to rewind to requested index.
        # First try the last known position
        if index >= self.last_known_pos_index:
            seek_index = self.last_known_pos_index
            seek_pos = self.last_known_pos

        # Last known position was too big. If
        # there is no lookup table or index is
        # too small, then start from beginning.
        elif not self.index_lookup or index < self.index_lookup_multiplier:
            seek_index = 0
            seek_pos = 0

        # Try from lookup table
        else:
            lookup_table_index = (index - self.index_lookup_multiplier) / self.index_lookup_multiplier
            # Lookup table index should always be small enough,
            # because if too big indices are requested, then
            # last_known_pos kicks in at the start.
            assert lookup_table_index < len(self.index_lookup)
            seek_index = (lookup_table_index + 1) * self.index_lookup_multiplier
            seek_pos = self.index_lookup[lookup_table_index]

        self.reader._seek(seek_pos)
        if seek_index == 0 and not self.reader._skip_if_next('['):
            raise Exception(u'Missing "["!')
        self.reader._skip_whitespace()

        if self.reader._is_next(']'):
            raise IndexError(u'Out of range!')

        while True:
            # If this is the requested element, then it doesn't
            # need to be read fully. If not, then its bytes
            # should be skipped, and it needs to be fully read.
            if index == seek_index:
                return self.reader.read(read_all=False)
            else:
                self.reader.read(read_all=True)

            # Skip comma and whitespace around it
            self.reader._skip_whitespace()
            if self.reader._skip_if_next(','):
                self.reader._skip_whitespace()
            elif self.reader._is_next(']'):
                raise IndexError(u'Out of range!')
            else:
                raise Exception(u'Expected "," or "]"!')

            seek_index += 1

            # Update lookup variables
            if seek_index > self.last_known_pos_index:
                self.last_known_pos_index = seek_index
                self.last_known_pos = self.reader._tell_read_pos()
            if seek_index == (len(self.index_lookup) + 1) * self.index_lookup_multiplier:
                self.index_lookup.append(self.reader._tell_read_pos())
                # If lookup table grows too big, half of its members will be removed
                if len(self.index_lookup) > Array._MAX_INDEX_LOOKUP_LENGTH:
                    self.index_lookup = [pos for i, pos in enumerate(self.index_lookup) if i % 2 == 1]
                    self.index_lookup_multiplier *= 2

    def __len__(self):
        if self.length < 0:
            self._read_all()
        return self.length


class FileReader:

    _WHITESPACE = '\t\n '
    _READBUF_CHUNK_SIZE = 1024*1024

    def __init__(self, file):
        self.file = file

        # This buffer is for reading and peeking
        self.readbuf = ''
        self.readbuf_read = 0
        self.readbuf_pos = 0

    def read(self, read_all=False, to_python=False):
        # "to_python" cannot be set without "read_all"
        assert read_all or not to_python

        self._skip_whitespace()

        # None
        if self._skip_if_next('null'):
            return None

        # False
        if self._skip_if_next('false'):
            return False

        # True
        if self._skip_if_next('true'):
            return True

        # Number
        if self._peek() in '-0123456789':
            num = self._get()
            # Check sign
            if num == '-':
                num += self._get()
            # Read integer part
            if num[-1] != '0':
                while self._peek() in '0123456789':
                    num += self._get()
            # Read possible decimal part and convert to float
            if self._peek() == '.':
                self._get()
                num += '.' + self._get()
                if num[-1] not in '01234567890':
                    raise Exception(u'Expected digit after dot!')
                while self._peek() in '0123456789':
                    num += self._get()
                num = float(num)
            else:
                num = int(num)
            # Read possible exponent
            if self._peek() in 'eE':
                self._get()
                exp = self._get()
                exp_neg = False
                if exp == '-':
                    exp_neg = True
                    exp = self._get()
                elif exp == '+':
                    exp = self._get()
                while self._peek() in '0123456789':
                    exp += self._get()
                exp = int(exp)
                exp = 10 ** exp
                if exp_neg:
                    num = float(num) / exp
                else:
                    num *= exp
            return num

        # String
        if self._skip_if_next('"'):
            string = u''

            while True:
                c = self._get()

                if c == u'"':
                    break

                if c == u'\\':
                    c = self._get()
                    if c == u'"':
                        string += u'"'
                    elif c == u'\\':
                        string += u'\\'
                    elif c == u'/':
                        string += u'/'
                    elif c == u'b':
                        string += u'\b'
                    elif c == u'f':
                        string += u'\f'
                    elif c == u'n':
                        string += u'\n'
                    elif c == u'r':
                        string += u'\r'
                    elif c == u't':
                        string += u'\t'
                    elif c == u'u':
                        unicode_bytes = self._read(4)
                        string += ('\\u' + unicode_bytes).encode('utf-8', errors="replace").decode('unicode_escape', errors="replace")
                    else:
                        raise Exception(u'Unexpected {} in backslash encoding!'.format(c))

                else:
                    string += c

            return string

        # Array
        if self._peek() == '[':
            if to_python:
                array = Array(self, False)
                return array.to_python()
            else:
                return Array(self, read_all)

        # Object
        if self._peek() == '{':
            if to_python:
                obj = Object(self, False)
                return obj.to_python()
            else:
                return Object(self, read_all)

        raise Exception(u'Unexpected bytes!')

    def _skip_whitespace(self):
        while True:
            self._ensure_readbuf_left(1)
            if len(self.readbuf) - self.readbuf_read < 1:
                break

            if self.readbuf[self.readbuf_read] not in FileReader._WHITESPACE:
                break

            self.readbuf_read += 1

    def _get(self):
        self._ensure_readbuf_left(1)
        if len(self.readbuf) - self.readbuf_read < 1:
            raise Exception(u'Unexpected end of file when getting next byte!')
        result = self.readbuf[self.readbuf_read]
        self.readbuf_read += 1
        return result

    def _read(self, amount):
        self._ensure_readbuf_left(amount)
        if len(self.readbuf) - self.readbuf_read < amount:
            raise Exception(u'Unexpected end of file reading a chunk!')
        result = self.readbuf[self.readbuf_read:self.readbuf_read+amount]
        self.readbuf_read += amount
        return result

    def _peek(self):
        self._ensure_readbuf_left(1)
        if len(self.readbuf) - self.readbuf_read < 1:
            return None
        return self.readbuf[self.readbuf_read]

    def _is_next(self, s):
        s_len = len(s)
        self._ensure_readbuf_left(s_len)
        if len(self.readbuf) - self.readbuf_read < s_len:
            return False
        for i in range(s_len):
            if self.readbuf[self.readbuf_read + i] != s[i]:
                return False
        return True

    def _skip_if_next(self, s):
        """ If next bytes are same as in 's', then skip them and return True.
        """
        if self._is_next(s):
            self.readbuf_read += len(s)
            return True
        return False

    def _ensure_readbuf_left(self, minimum_left):
        if len(self.readbuf) - self.readbuf_read >= minimum_left:
            return
        read_amount = max(minimum_left, FileReader._READBUF_CHUNK_SIZE) - (len(self.readbuf) - self.readbuf_read)
        self.readbuf_pos += self.readbuf_read
        self.readbuf = self.readbuf[self.readbuf_read:] + self.file.read(read_amount).decode('utf-8', errors='replace')
        self.readbuf_read = 0

    def _tell_read_pos(self):
        return self.readbuf_pos + self.readbuf_read

    def _seek(self, pos):
        # If position is at the area of
        # readbuffer, then just rewind it.
        if pos >= self.readbuf_pos and pos <= self.readbuf_pos + len(self.readbuf):
            self.readbuf_read = pos - self.readbuf_pos
        # If position is outside the readbuffer,
        # then read buffer from scratch
        else:
            self.readbuf = ''
            self.readbuf_read = 0
            self.readbuf_pos = pos
            self.file.seek(pos)


class SFBulkHandler(object):
    """ Bulk API request handler
    Intermediate class which allows us to use commands,
     such as 'sf.bulk.Contacts.create(...)'
    This is really just a middle layer, whose sole purpose is
    to allow the above syntax
    """

    def __init__(self, session_id, bulk_url, proxies=None, session=None, calls_logger=None):
        """Initialize the instance with the given parameters.

        Arguments:

        * session_id -- the session ID for authenticating to Salesforce
        * bulk_url -- API endpoint set in Salesforce instance
        * proxies -- the optional map of scheme to proxy server
        * session -- Custom requests session, created in calling code. This
                     enables the use of requests Session features not otherwise
                     exposed by simple_salesforce.
        """
        self.session_id = session_id
        self.session = session or requests.Session()
        self.bulk_url = bulk_url
        # don't wipe out original proxies with None
        if not session and proxies is not None:
            self.session.proxies = proxies

        self.calls_logger = calls_logger

        # Define these headers separate from Salesforce class,
        # as bulk uses a slightly different format
        self.headers = {
            'Content-Type': 'application/json',
            'X-SFDC-Session': self.session_id,
            'X-PrettyPrint': '1'
        }

    def __getattr__(self, name):
        return SFBulkType(object_name=name, bulk_url=self.bulk_url,
                          headers=self.headers, session=self.session,
                          calls_logger=self.calls_logger)


class SFBulkType(object):
    """ Interface to Bulk/Async API functions"""

    def __init__(self, object_name, bulk_url, headers, session, calls_logger):
        """Initialize the instance with the given parameters.

        Arguments:

        * object_name -- the name of the type of SObject this represents,
                         e.g. `Lead` or `Contact`
        * bulk_url -- API endpoint set in Salesforce instance
        * headers -- bulk API headers
        * session -- Custom requests session, created in calling code. This
                     enables the use of requests Session features not otherwise
                     exposed by simple_salesforce.
        """
        self.object_name = object_name
        self.bulk_url = bulk_url
        self.session = session
        self.headers = headers
        self.calls_logger = calls_logger

    def _call_salesforce(self, url, method, session, headers, res_to_json=True, **kwargs):
        """Utility method for performing HTTP call to Salesforce.

        Returns a `requests.result` object.
        """

        additional_headers = kwargs.pop('additional_headers', dict())
        headers.update(additional_headers or dict())
        result = session.request(method, url, headers=headers, **kwargs)

        if result.status_code >= 300:
            if self.calls_logger is not None:
                self.calls_logger.add_metric(url, method, None)
            exception_handler(result)

        if res_to_json and self.calls_logger is not None:
            # row counts are removed to fight memory issues
            #json_res = result.json(object_pairs_hook=OrderedDict)
            #row_count = len(json_res)
            # if isinstance(json_res, dict) and json_res.get("fields") is not None:
            #     row_count = len(json_res.get("fields"))
            # if isinstance(json_res, dict) and json_res.get("records") is not None:
            #     row_count = len(json_res.get("records"))
            self.calls_logger.add_metric(url, method, None)

        return result

    def _create_job(self, operation, object_name, external_id_field=None, fp=None, chunk_size=100000):
        """ Create a bulk job

        Arguments:

        * operation -- Bulk operation to be performed by job
        * object_name -- SF object
        * external_id_field -- unique identifier field for upsert operations
        """

        payload = {
            'operation': operation,
            'object': object_name,
            'contentType': 'JSON'
        }

        additional_headers = {}

        if operation == 'upsert':
            payload['externalIdFieldName'] = external_id_field

        if operation == 'query' and chunk_size is not None:
            additional_headers = {
                'Sforce-Enable-PKChunking': 'chunkSize={}'.format(chunk_size)
            }

        url = "{}{}".format(self.bulk_url, 'job')

        result = self._call_salesforce(url=url, method='POST', session=self.session,
                                       headers=self.headers,
                                       data=json.dumps(payload),
                                       additional_headers=additional_headers)

        return result.json(object_pairs_hook=OrderedDict)

    def _close_job(self, job_id):
        """ Close a bulk job """
        payload = {
            'state': 'Closed'
        }

        url = "{}{}{}".format(self.bulk_url, 'job/', job_id)

        result = self._call_salesforce(url=url, method='POST', session=self.session,
                                       headers=self.headers,
                                       data=json.dumps(payload))
        return result.json(object_pairs_hook=OrderedDict)

    def _get_job(self, job_id):
        """ Get an existing job to check the status """
        url = "{}{}{}".format(self.bulk_url, 'job/', job_id)

        result = self._call_salesforce(url=url, method='GET', session=self.session,
                                       headers=self.headers)
        return result.json(object_pairs_hook=OrderedDict)

    def _add_batch(self, job_id, data, operation):
        """ Add a set of data as a batch to an existing job
        Separating this out in case of later
        implementations involving multiple batches
        """

        url = "{}{}{}{}".format(self.bulk_url, 'job/', job_id, '/batch')

        if operation != 'query':
            data = json.dumps(data)

        result = self._call_salesforce(url=url, method='POST', session=self.session,
                                       headers=self.headers, data=data)

        return result.json(object_pairs_hook=OrderedDict)

    def _get_batch(self, job_id, batch_id):
        """ Get an existing batch to check the status """

        url = "{}{}{}{}{}".format(self.bulk_url, 'job/',
                                  job_id, '/batch/', batch_id)

        result = self._call_salesforce(url=url, method='GET', session=self.session,
                                       headers=self.headers)
        return result.json(object_pairs_hook=OrderedDict)

    def _get_batches(self, job_id, batch_id=None):
        """ Get an existing batch to check the status """
        if batch_id is not None:
            return [self._get_batch(job_id=job_id, batch_id=batch_id)]

        url = "{}{}{}{}".format(self.bulk_url, 'job/', job_id, '/batch')

        result = self._call_salesforce(url=url, method='GET', session=self.session,
                                       headers=self.headers)

        return result.json(object_pairs_hook=OrderedDict)['batchInfo']

    def _get_batch_results(self, job_id, batch_id, operation, fp=None):
        """ retrieve a set of results from a completed job """

        url = "{}{}{}{}{}{}".format(self.bulk_url, 'job/', job_id, '/batch/',
                                    batch_id, '/result')




        result = self._call_salesforce(url=url, method='GET', session=self.session,
                                       headers=self.headers)

        res_js = result.json()

        logging.info("Batch result response is " + json.dumps(res_js))

        if operation == 'query':
            total_res = []
            tmp_name = None
            try:
                with NamedTemporaryFile("ab", delete=False) as tmp_file:
                    tmp_name = tmp_file.name
                    for elem in res_js:
                        url_query_results = "{}{}{}".format(url, '/', elem)
                        logging.info("Sending query to salesforce to get the batch {}".format(elem))
                        headers = self.headers
                        streaming = True if fp is not None else False
                        with self.session.request("GET", url_query_results, headers=headers, stream=streaming) as query_result:

                            if result.status_code >= 300:
                                if self.calls_logger is not None:
                                    self.calls_logger.add_metric(url, "GET", None)
                                exception_handler(result)

                            if self.calls_logger is not None:
                                self.calls_logger.add_metric(url, "GET", None)

                            logging.info("Batch {} is obtained".format(elem))
                            if fp is not None:
                                for chunk in query_result.iter_content(chunk_size=1024000):
                                    if chunk:  # filter out keep-alive new chunks
                                        tmp_file.write(chunk)

                                logging.info("Batch {} is streamed to disk".format(elem))
                            else:
                                json_res = query_result.json()
                                total_res.extend(json_res)
                                logging.info("Batch {} is added to result list".format(elem))

                if fp is not None:
                    with open(tmp_name, "rb") as tmp_file:
                        with open(fp, 'a', encoding="utf-8") as jfile:
                            reader = FileReader(tmp_file)
                            records = reader.read()
                            for record in records:
                                dct = record.to_python()
                                jfile.write(json.dumps(dct, ensure_ascii=False) + '\n')
                    return True
                return total_res
            finally:
                if tmp_name is not None:
                    os.remove(tmp_name)

        return res_js

    def _monitor_batches(self, job_id, batch_id=None, wait=5):
        """ monitor a job's batches
        """
        complete_states = {'Completed', 'Failed', 'NotProcessed'}

        batches = self._get_batches(job_id=job_id, batch_id=batch_id)

        batch_states = set([batch['state'] for batch in batches])

        while not batch_states.issubset(complete_states):
            sleep(wait)
            batches = self._get_batches(job_id=job_id, batch_id=batch_id)
            states_tmp = [batch['state'] for batch in batches]
            batch_states = set(states_tmp)
            logging.info("Batches states are " + json.dumps(states_tmp))

        return True

    # pylint: disable=R0913
    def _bulk_operation(self, object_name, operation, data,
                        external_id_field=None, wait=60, fp=None, chunk_size=100000):
        """ String together helper functions to create a complete
        end-to-end bulk API request

        Arguments:

        * object_name -- SF object
        * operation -- Bulk operation to be performed by job
        * data -- list of dict to be passed as a batch
        * external_id_field -- unique identifier field for upsert operations
        * wait -- seconds to sleep between checking batch status
        """
        job = self._create_job(
            object_name=object_name,
            operation=operation,
            external_id_field=external_id_field,
            fp=fp,
            chunk_size=chunk_size
        )

        logging.info("Bulk job is created")

        init_batch = self._add_batch(job_id=job['id'], data=data,
                                     operation=operation)

        logging.info("Initial Batch is added")

        self._monitor_batches(job_id=job['id'], batch_id=init_batch['id'], wait=wait)

        self._close_job(job_id=job['id'])

        self._monitor_batches(job_id=job['id'], wait=wait)

        logging.info("Monitoring of batches has ended")

        batches = self._get_batches(job['id'])

        if fp is not None and chunk_size is not None:
            for batch in batches:
                if batch['id'] == init_batch['id']:
                    continue

                self._get_batch_results(job_id=init_batch['jobId'],
                                        batch_id=batch['id'],
                                        operation=operation,
                                        fp=fp)

            return True

        results = self._get_batch_results(job_id=init_batch['jobId'],
                                          batch_id=init_batch['id'],
                                          operation=operation,
                                          fp=fp)
        return results

    # _bulk_operation wrappers to expose supported Salesforce bulk operations
    def delete(self, data):
        """ soft delete records """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='delete', data=data)
        return results

    def insert(self, data):
        """ insert records """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='insert', data=data)
        return results

    def upsert(self, data, external_id_field):
        """ upsert records based on a unique identifier """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='upsert',
                                       external_id_field=external_id_field,
                                       data=data)
        return results

    def update(self, data):
        """ update records """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='update', data=data)
        return results

    def hard_delete(self, data):
        """ hard delete records """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='hardDelete', data=data)
        return results

    def query(self, data, fp=None, chunk_size=100000, wait=60):
        """ bulk query """
        results = self._bulk_operation(object_name=self.object_name,
                                       operation='query', data=data,
                                       fp=fp, chunk_size=chunk_size,
                                       wait=wait)
        return results
