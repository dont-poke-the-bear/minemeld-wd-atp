import logging
import os
import shutil
import time
import uuid
import netaddr
from datetime import datetime, timedelta
from collections import deque

import adal  #pylint: disable=E0401
import gevent
import requests
import yaml
import ujson as json
from gevent.queue import Queue, Empty, Full
from netaddr import IPNetwork
from requests.exceptions import RequestException, HTTPError

from minemeld.ft import ft_states  #pylint: disable=E0401
from minemeld.ft.base import _counting  #pylint: disable=E0401
from minemeld.ft.actorbase import ActorBaseFT  #pylint: disable=E0401
from minemeld.ft.table import Table #pylint: disable=E0401

LOG = logging.getLogger(__name__)
AUTHORITY_BASE_URL = 'https://login.microsoftonline.com'
AUTHORITY_URL = 'https://login.microsoftonline.com/{}'
RESOURCE = 'https://securitycenter.onmicrosoft.com/windowsatpservice'
ENDPOINT_URL = 'https://partnerstifrontend-eus-prd.trafficmanager.net/threatintel/info'

MM_2_WDATP_TYPE = {
    'md5': 'FileMd5',
    'sha1': 'FileSha1',
    'sha256': 'FileSha256',
    'IPv4': 'IpAddress',
    'domain': 'DomainName',
    'URL': 'Url'
}
WD_ATP_TIINDICATORS_ENDPOINT = 'api.securitycenter.windows.com'

class AuthConfigException(RuntimeError):
    pass

class WDATPResponseException(RuntimeError):
    pass

class Output(ActorBaseFT):
    def __init__(self, name, chassis, config):
        self._queue = None

        super(Output, self).__init__(name, chassis, config)

        self._push_glet = None
        self._checkpoint_glet = None
        self.api_client_id = str(uuid.uuid4())
        self.sequence_number = 0

    def configure(self):
        super(Output, self).configure()

        self.queue_maxsize = int(self.config.get('queue_maxsize', 100000))
        if self.queue_maxsize == 0:
            self.queue_maxsize = None
        self._queue = Queue(maxsize=self.queue_maxsize)

        self.client_id = self.config.get('client_id', None)
        self.client_secret = self.config.get('client_secret', None)
        self.tenant_id = self.config.get('tenant_id', None)

        self.sender_id = self.config.get('sender_id', 'minemeld')

        self.side_config_path = self.config.get('side_config', None)
        if self.side_config_path is None:
            self.side_config_path = os.path.join(
                os.environ['MM_CONFIG_DIR'],
                '%s_side_config.yml' % self.name
            )

        self._load_side_config()

    def _load_side_config(self):
        try:
            with open(self.side_config_path, 'r') as f:
                sconfig = yaml.safe_load(f)

        except Exception as e:
            LOG.error('%s - Error loading side config: %s', self.name, str(e))
            return

        client_id = sconfig.get('client_id', None)
        if client_id is not None:
            self.client_id = client_id
            LOG.info('{} - client_id set'.format(self.name))

        client_secret = sconfig.get('client_secret', None)
        if client_secret is not None:
            self.client_secret = client_secret
            LOG.info('{} - client_secret set'.format(self.name))

        tenant_id = sconfig.get('tenant_id', None)
        if tenant_id is not None:
            self.tenant_id = tenant_id
            LOG.info('{} - tenant_id set'.format(self.name))

    def _saved_state_restore(self, saved_state):
        super(Output, self)._saved_state_restore(saved_state)

        self.api_client_id = saved_state.get('api_client_id', None)
        self.sequence_number = saved_state.get('sequence_number', None)

        LOG.info('{} - saved state: api_client_id: {} sequence_number: {}'.format(
            self.name,
            self.api_client_id,
            self.sequence_number
        ))

    def _saved_state_create(self):
        sstate = super(Output, self)._saved_state_create()

        sstate['api_client_id'] = self.api_client_id
        sstate['sequence_number'] = self.sequence_number

        return sstate

    def _saved_state_reset(self):
        super(Output, self)._saved_state_reset()

        self.api_client_id = str(uuid.uuid4())
        self.sequence_number = 0

    def connect(self, inputs, output):
        output = False
        super(Output, self).connect(inputs, output)

    def initialize(self):
        pass

    def rebuild(self):
        pass

    def reset(self):
        pass

    def _get_auth_token(self):
        if self.client_id is None:
            LOG.error('{} - client_id not set'.format(self.name))
            raise AuthConfigException('{} - client_id not set'.format(self.name))
        if self.client_secret is None:
            LOG.error('{} - client_secret not set'.format(self.name))
            raise AuthConfigException('{} - client_secret not set'.format(self.name))
        if self.tenant_id is None:
            LOG.error('{} - tenant_id not set'.format(self.name))
            raise AuthConfigException('{} - tenant_id not set'.format(self.name))

        context = adal.AuthenticationContext(
            AUTHORITY_URL.format(self.tenant_id),
            validate_authority=self.tenant_id != 'adfs',
            api_version=None
        )

        token = context.acquire_token_with_client_credentials(
            RESOURCE,
            self.client_id,
            self.client_secret
        )

        if token is None or 'accessToken' not in token:
            LOG.error('{} - Invalid token or accessToken not available'.format(self.name))
            raise RuntimeError('{} - Invalid token or accessToken not available'.format(self.name))

        return token['accessToken']

    def _get_endpoint_orgid(self, token):
        # this should look like 
        # {
        # u'AadTenantId': u'bb19bb5c-0e8d-4a73-bd6b-d015b298ecd7',
        # u'ServiceUri': u'https://partnerstifrontend-eus-prd.trafficmanager.net/threatintel/indicators',
        # u'ServiceType': 1,
        # u'WdAtpOrgId': u'55c01df7-a1eb-4eae-ae3b-a9b423d07d72'
        # }
        result = requests.get(
            ENDPOINT_URL,
            headers={
                'Authorization': 'Bearer {}'.format(token),
                'Content-Type': 'application/json'
            }
        )
        result.raise_for_status()

        result = result.json()
        LOG.debug('{} - endpoints: {}'.format(self.name, result))

        if result.get('AadTenantId', None) != self.tenant_id:
            raise AuthConfigException('{} - Endpoint response AadTenantId differs from tenant_id: {}'.format(self.name, result))

        endpoint = result.get('ServiceUri', None)
        if endpoint is None:
            raise AuthConfigException('{} - Endpoint response missing ServiceUri field: {}'.format(self.name, result))

        org_id = result.get('WdAtpOrgId', None)
        if endpoint is None:
            raise AuthConfigException('{} - Endpoint response missing WdAtpOrgId field: {}'.format(self.name, result))

        return endpoint, org_id

    def _push_indicators(self, token, endpoint, org_id, indicators):
        # DEPRECATED

        # message = {
        #     'Id': self.api_client_id,
        #     'SequenceNumber': self.sequence_number,
        #     'SenderId': self.sender_id,
        #     'Indicators': list(indicators),
        #     'WdAtpOrgId': org_id
        # }

        # LOG.debug(message)

        # result = requests.post(
        #     endpoint,
        #     headers={
        #         'Content-Type': 'application/json',
        #         'Authorization': 'Bearer {}'.format(token)
        #     },
        #     json=message
        # )

        # LOG.debug(result.text)

        # result.raise_for_status()
        raise WDATPResponseException('This output node is deprecated: please switch to OutputBatch')

    def _push_loop(self):
        while True:
            msg = self._queue.get()

            artifacts = deque()
            artifacts.append(msg)

            try:
                while len(artifacts) < 511:
                    artifacts.append(self._queue.get_nowait())
            except Empty:
                pass

            while True:
                result = None

                try:
                    LOG.info('{} - Sending {}:{}'.format(self.name, self.api_client_id, self.sequence_number))
                    # DEPRECATED - no need to get the token
                    # token = self._get_auth_token()
                    token = 'DEPRECATED'
                    LOG.debug('{} - token: {}'.format(self.name, token))

                    # DEPRECATED
                    #endpoint, org_id = self._get_endpoint_orgid(token)
                    #LOG.debug('{} - endpoint: {} WdAtpOrgId: {}'.format(self.name, endpoint, org_id))

                    # self._push_indicators(
                    #     token=token,
                    #     endpoint=endpoint,
                    #     org_id=org_id,
                    #     indicators=artifacts
                    # )
                    self._push_indicators(None, None, None, None)

                    self.sequence_number += 1
                    self.statistics['indicator.tx'] += len(artifacts)
                    break

                except gevent.GreenletExit:
                    return

                except RequestException as e:
                    LOG.error('{} - error submitting indicators - {}'.format(self.name, str(e)))

                    if result is not None and result.status_code >= 400 and result.status_code < 500:
                        LOG.error('{}: error in request - {}'.format(self.name, result.text))
                        self.statistics['error.invalid_request'] += 1
                        break

                    self.statistics['error.submit'] += 1
                    gevent.sleep(60)

                except AuthConfigException as e:
                    LOG.exception('{} - Error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    gevent.sleep(60.0)

                except WDATPResponseException as e:
                    LOG.exception('{} - error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    break

                except Exception as e:
                    LOG.exception('{} - error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    gevent.sleep(120.0)

            gevent.sleep(0.1)

    def _encode_indicator(self, indicator, value, expired=False):
        type_ = value['type']

        if type_ not in [ 'URL', 'domain', 'md5', 'sha256', 'IPv4' ]:
            self.statistics['error.unhandled_type'] += 1
            raise RuntimeError('{} - Unhandled {}'.format(self.name, type_))

        description = '{} indicator from {}'.format(
            type_,
            ', '.join(value['sources'])
        )
        external_id = '{}:{}'.format(type_, indicator)
        expiration = datetime.utcnow() + timedelta(days=365)
        if expired:
            expiration = datetime.fromtimestamp(0)
        expiration = expiration.isoformat()

        indicators = []
        if type_ == 'IPv4' and '-' in indicator:
            # October 2020
            # as MSFT removed the support for CIDRs in the API
            # we must translate the ranges in individual IPv4 indicators
            a1, a2 = indicator.split('-', 1)
            r = netaddr.IPRange(a1, a2)
            indicators = [str(i) for i in r]
        else:
            indicators = [indicator]


        result = []
        for i in indicators:
            r = {
                'Description': description,
                'Confidence': value['confidence'],
                'ExternalId': external_id,
                'IndicatorExpirationDateTime': expiration
            }
            if type_ == 'URL':
                r['Url'] = indicator
            elif type_ == 'domain':
                r['DNSDomainName'] = indicator
            elif type_ == 'md5':
                r['FileMD5'] = indicator
            elif type_ == 'sha256':
                r['FileSha256'] = indicator
            elif type_ == 'IPv4':
                r['NetworkDestinationIPv4'] = str(indicator)
            else:
                # Unsupported indicator type, should never reach this code
                continue

            LOG.debug('{!r} - add indicator {!r} to queue'.format(self.name, r))
            result.append(r)

        return result

    def _checkpoint_check(self, source=None, value=None):
        t0 = time.time()

        while ((time.time() - t0) < 30) and self._queue.qsize() != 0:
            gevent.sleep(0.5)
        self._push_glet.kill()

        LOG.info('{} - checkpoint with {} elements in the queue'.format(self.name, self._queue.qsize()))
        super(Output, self).checkpoint(source=source, value=value)

    @_counting('update.processed')
    def filtered_update(self, source=None, indicator=None, value=None):
        try:
            for i in self._encode_indicator(indicator, value, expired=False):
                self._queue.put(
                    i,
                    block=True,
                    timeout=0.001
                )
        except Full:
            self.statistics['error.queue_full'] += 1

    @_counting('withdraw.processed')
    def filtered_withdraw(self, source=None, indicator=None, value=None):
        if value is None:
            self.statistics['error.no_value'] += 1
            return

        try:
            for i in self._encode_indicator(indicator, value, expired=True):
                self._queue.put(
                    i,
                    block=True,
                    timeout=0.001
            )
        except Full:
            self.statistics['error.queue_full'] += 1

    @_counting('checkpoint.rx')
    def checkpoint(self, source=None, value=None):
        self.state = ft_states.CHECKPOINT
        self._checkpoint_glet = gevent.spawn(
            self._checkpoint_check,
            source,
            value
        )

    def mgmtbus_status(self):
        result = super(ActorBaseFT, self).mgmtbus_status()
        result['sub_state'] = 'ERROR'
        result['sub_state_message'] = 'This node is deprecated'

        return result

    def length(self, source=None):
        return self._queue.qsize()

    def start(self):
        super(Output, self).start()

        self._push_glet = gevent.spawn(self._push_loop)

    def stop(self):
        super(Output, self).stop()

        if self._push_glet is not None:
            self._push_glet.kill()

        if self._checkpoint_glet is not None:
            self._checkpoint_glet.kill()

    def hup(self, source=None):
        LOG.info('%s - hup received, reload side config', self.name)
        self._load_side_config()

    @staticmethod
    def gc(name, config=None):
        ActorBaseFT.gc(name, config=config)
        shutil.rmtree(name, ignore_errors=True)


class OutputBatch(ActorBaseFT):
    def __init__(self, name, chassis, config):
        self._queue = None

        super(OutputBatch, self).__init__(name, chassis, config)

        self._push_glet = None
        self._checkpoint_glet = None

    def configure(self):
        super(OutputBatch, self).configure()

        self.queue_maxsize = int(self.config.get('queue_maxsize', 100000))
        if self.queue_maxsize == 0:
            self.queue_maxsize = None
        self._queue = Queue(maxsize=self.queue_maxsize)

        self.client_id = self.config.get('client_id', None)
        self.client_secret = self.config.get('client_secret', None)
        self.tenant_id = self.config.get('tenant_id', None)
        self.action = self.config.get('action', 'Alert')
        self.severity = self.config.get('severity', None)
        self.ioc_endpoint = self.config.get('ioc_endpoint', None)

        self.side_config_path = self.config.get('side_config', None)
        if self.side_config_path is None:
            self.side_config_path = os.path.join(
                os.environ['MM_CONFIG_DIR'],
                '%s_side_config.yml' % self.name
            )

        self._load_side_config()

    def _load_side_config(self):
        try:
            with open(self.side_config_path, 'r') as f:
                sconfig = yaml.safe_load(f)

        except Exception as e:
            LOG.error('%s - Error loading side config: %s', self.name, str(e))
            return

        client_id = sconfig.get('client_id', None)
        if client_id is not None:
            self.client_id = client_id
            LOG.info('{} - client_id set'.format(self.name))

        client_secret = sconfig.get('client_secret', None)
        if client_secret is not None:
            self.client_secret = client_secret
            LOG.info('{} - client_secret set'.format(self.name))

        tenant_id = sconfig.get('tenant_id', None)
        if tenant_id is not None:
            self.tenant_id = tenant_id
            LOG.info('{} - tenant_id set'.format(self.name))

        action = sconfig.get('action', None)
        if action is not None:
            self.action = action
            LOG.info('{} - action set'.format(self.action))

        severity = sconfig.get('severity', None)
        if severity is not None:
            self.severity = severity
            LOG.info('{} - severity set'.format(self.name))

        ioc_endpoint = sconfig.get('ioc_endpoint', WD_ATP_TIINDICATORS_ENDPOINT)
        if ioc_endpoint is not None:
            self.ioc_endpoint = "https://{}/api/indicators/import".format(ioc_endpoint)
            LOG.info('{} - ioc_endpoint set'.format(self.name))

    def connect(self, inputs, output):
        output = False
        super(OutputBatch, self).connect(inputs, output)

    def _initialize_table(self, truncate=False):
        self.table = Table(name=self.name, truncate=truncate)

    def initialize(self):
        self._initialize_table()

    def rebuild(self):
        self._initialize_table(truncate=(self.last_checkpoint is None))

    def reset(self):
        self._initialize_table(truncate=True)

    def _get_auth_token(self):
        if self.client_id is None:
            LOG.error('{} - client_id not set'.format(self.name))
            raise AuthConfigException('{} - client_id not set'.format(self.name))
        if self.client_secret is None:
            LOG.error('{} - client_secret not set'.format(self.name))
            raise AuthConfigException('{} - client_secret not set'.format(self.name))
        if self.tenant_id is None:
            LOG.error('{} - tenant_id not set'.format(self.name))
            raise AuthConfigException('{} - tenant_id not set'.format(self.name))

        context = adal.AuthenticationContext(
            AUTHORITY_URL.format(self.tenant_id),
            validate_authority=self.tenant_id != 'adfs',
            api_version=None
        )

        token = context.acquire_token_with_client_credentials(
            RESOURCE,
            self.client_id,
            self.client_secret
        )

        if token is None or 'accessToken' not in token:
            LOG.error('{} - Invalid token or accessToken not available'.format(self.name))
            raise RuntimeError('{} - Invalid token or accessToken not available'.format(self.name))

        return token['accessToken']

    def _push_indicators(self, token, indicators):
        message = {
            'Indicators': list(indicators)
        }

        LOG.debug(message)

        result = requests.post(
            self.ioc_endpoint,
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer {}'.format(token)
            },
            json=message
        )

        LOG.debug(result.text)

        result.raise_for_status()

        # Check the status of the submitted indicators
        # NOTE: if the indicator contains a range split by _encode_indicators, a partial submission might go through
        # i.e. 192.168.0.1-192.168.0.3 can be split in 192.168.0.1, 192.168.0.2 and 192.168.0.3
        # the first might go through, the second might return error
        # This output node doesn't check for this condition (although the error counters are correctly updated)

        result = result.json()
        if not result or  '@odata.context' not in result or '/api/$metadata#Collection(microsoft.windowsDefenderATP.api.ImportIndicatorResult)' not in result['@odata.context']:
            raise WDATPResponseException('Unexpected response from WDATP API')

        if 'value' not in result:
            raise WDATPResponseException('Missing value from WDATP API result')

        for v in result['value']:
            if 'indicator' not in v or 'isFailed' not in v:
                raise WDATPResponseException('Missing indicator values from WDATP response')
            LOG.debug('{} - Got result for indicator {}: isFailed is {}'.format(self.name, v['indicator'], v["isFailed"]))
            if not v["isFailed"]:
                # Success!
                self.statistics['indicator.tx'] += 1
            else:
                failReason = v['failureReason'] if 'failureReason' in v else 'Unknown'
                LOG.error('{}: error submitting indicator {}: {}'.format(self.name, v['indicator'], failReason))
                self.statistics['error.submit'] += 1

    def _push_loop(self):
        while True:
            msg = self._queue.get()

            artifacts = deque()
            artifacts.append(msg)

            try:
                while len(artifacts) < 50:
                    artifacts.append(self._queue.get_nowait())
            except Empty:
                pass

            while True:
                retries = 0

                try:
                    LOG.info('{} - Sending {} indicators'.format(self.name, len(artifacts)))
                    token = self._get_auth_token()
                    LOG.debug('{} - token: {}'.format(self.name, token))

                    self._push_indicators(
                        token=token,
                        indicators=artifacts
                    )
                    # Counter already incremented in push_indicators
                    # self.statistics['indicator.tx'] += len(artifacts)
                    break

                except gevent.GreenletExit:
                    return

                except HTTPError as e:
                    LOG.error('{} - error submitting indicators - {}'.format(self.name, str(e)))
                    status_code = e.response.status_code

                    if status_code >= 400 and status_code < 500:
                        LOG.error('{}: error in request - {}'.format(self.name, e.response.text))
                        self.statistics['error.invalid_request'] += 1
                        break

                    self.statistics['error.submit'] += 1
                    gevent.sleep(60)

                except AuthConfigException as e:
                    LOG.exception('{} - Error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    gevent.sleep(60.0)

                except WDATPResponseException as e:
                    LOG.exception('{} - error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    break                    
                    
                except Exception as e:
                    LOG.exception('{} - error submitting indicators - {}'.format(self.name, str(e)))
                    self.statistics['error.submit'] += 1
                    retries += 1
                    if retries > 5:
                        break
                    gevent.sleep(120.0)

            gevent.sleep(0.1)

    def _encode_indicator(self, indicator, value, expired=False):
        type_ = MM_2_WDATP_TYPE.get(
            value['type'],
            None
        )
        if type_ is None:
            self.statistics['error.unhandled_type'] += 1
            raise RuntimeError('{} - Unhandled {}'.format(self.name, type_))

        if value['type'] == 'IPv4' and '-' in indicator:
            a1, a2 = indicator.split('-', 1)
            r = netaddr.IPRange(a1, a2)
            indicators = [str(i) for i in r]
        else:
            indicators = [indicator]

        description = '{} indicator from {}'.format(
            type_,
            ', '.join(value['sources'])
        )
        title = 'MineMeld - {}'.format(indicator)

        creation = datetime.utcnow()
        creation = creation.isoformat() + 'Z'

        # Set Action to Block and Remediate for Hash types based on fall '21 schema update
        if "File" in type_ and self.action == 'Block':
            self.action = 'BlockAndRemediate'

        #expiration = datetime.utcnow() + timedelta(days=365)


        result = []
        for i in indicators:
            d = dict(
                indicatorValue=i,
                indicatorType=type_,
                title=title,
                description=description,
                creationTimeDateTimeUtc=creation,
                action=self.action,
                generateAlert=True
            )
            if self.severity is not None:
                d['severity'] = self.severity

            if expired:
                expiration = datetime.utcnow() + timedelta(seconds=300)
                expiration = expiration.isoformat() + 'Z' # expiration is always in UTC
                d['expirationTime'] = expiration

            result.append(d)

        return result

    def _checkpoint_check(self, source=None, value=None):
        t0 = time.time()

        while ((time.time() - t0) < 30) and self._queue.qsize() != 0:
            gevent.sleep(0.5)
        self._push_glet.kill()

        LOG.info('{} - checkpoint with {} elements in the queue'.format(self.name, self._queue.qsize()))
        super(OutputBatch, self).checkpoint(source=source, value=value)

    @_counting('update.processed')
    def filtered_update(self, source=None, indicator=None, value=None):
        try:
            for i in self._encode_indicator(indicator, value, expired=False):
                self._queue.put(
                    i,
                    block=True,
                    timeout=0.001
                )
        except Full:
            self.statistics['error.queue_full'] += 1

    @_counting('withdraw.processed')
    def filtered_withdraw(self, source=None, indicator=None, value=None):
        if value is None:
            self.statistics['error.no_value'] += 1
            return

        try:
            for i in self._encode_indicator(indicator, value, expired=True):
                self._queue.put(
                    i,
                    block=True,
                    timeout=0.001
                )
        except Full:
            self.statistics['error.queue_full'] += 1

    @_counting('checkpoint.rx')
    def checkpoint(self, source=None, value=None):
        self.state = ft_states.CHECKPOINT
        self._checkpoint_glet = gevent.spawn(
            self._checkpoint_check,
            source,
            value
        )

    def length(self, source=None):
        return self._queue.qsize()

    def start(self):
        super(OutputBatch, self).start()

        self._push_glet = gevent.spawn(self._push_loop)

    def stop(self):
        super(OutputBatch, self).stop()

        if self._push_glet is not None:
            self._push_glet.kill()

        if self._checkpoint_glet is not None:
            self._checkpoint_glet.kill()

        self.table.close()

    def hup(self, source=None):
        LOG.info('%s - hup received, reload side config', self.name)
        self._load_side_config()

    @staticmethod
    def gc(name, config=None):
        ActorBaseFT.gc(name, config=config)
        shutil.rmtree(name, ignore_errors=True)
