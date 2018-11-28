""" Classes for interacting with Salesforce Bulk API """
import logging

try:
    from collections import OrderedDict
except ImportError:
    # Python < 2.7
    from ordereddict import OrderedDict

import json
from time import sleep

import requests

from simple_salesforce.util import exception_handler


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

        if operation == 'query' and fp is not None:
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

        if operation == 'query':
            total_res = []
            for elem in res_js:
                url_query_results = "{}{}{}".format(url, '/', elem)
                logging.info("Sending query to salesforce to get the batch {}".format(elem))
                query_result = self._call_salesforce(url=url_query_results, method='GET',
                                                     session=self.session,
                                                     headers=self.headers)
                logging.info("Batch {} is obtained".format(elem))
                json_res = query_result.json()
                if fp is not None:
                    with open(fp, 'a') as jfile:
                        for record in json_res:
                            jfile.write(json.dumps(record, ensure_ascii=False) + '\n')
                    logging.info("Batch {} is streamed to disk".format(elem))
                else:
                    total_res.extend(json_res)
                    logging.info("Batch {} is added to result list".format(elem))
            if fp is not None:
                return True
            return total_res

        return result.json()

    def _monitor_batches(self, job_id, batch_id=None, wait=5):
        """ monitor a job's batches
        """
        complete_states = {'Completed', 'Failed', 'NotProcessed'}

        batches = self._get_batches(job_id=job_id, batch_id=batch_id)

        batch_states = set([batch['state'] for batch in batches])

        while not batch_states.issubset(complete_states):
            sleep(wait)
            batches = self._get_batches(job_id=job_id, batch_id=batch_id)
            batch_states = set([batch['state'] for batch in batches])

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
