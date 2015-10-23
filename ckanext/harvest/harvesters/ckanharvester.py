import urllib2

from ckan.lib.base import c
from ckan import model
from ckan.model import Session, Package
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.helpers import json
from ckan.lib.munge import munge_name
from ckan.plugins import toolkit
from urlparse import urljoin

from ckanext.harvest.model import HarvestJob, HarvestObject

import logging
log = logging.getLogger(__name__)

from base import HarvesterBase


class CKANHarvester(HarvesterBase):
    '''
    A Harvester for CKAN instances
    '''
    config = None

    api_version = 2
    action_api_version = 3

    def _get_action_api_offset(self):
        return '/api/%d/action' % self.action_api_version

    def _get_search_api_offset(self):
        return "%s/package_search" % self._get_action_api_offset()

    def _get_content(self, url):
        http_request = urllib2.Request(
            url = url,
        )

        api_key = self.config.get('api_key',None)
        if api_key:
            http_request.add_header('Authorization',api_key)

        try:
            http_response = urllib2.urlopen(http_request)
        except urllib2.URLError, e:
            raise ContentFetchError(
                'Could not fetch url: %s, error: %s' %
                (url, str(e))
            )
        return http_response.read()

    def _get_group(self, base_url, group_name):
        url = base_url + self._get_action_api_offset() + '/group_show?id=' + munge_name(group_name)
        try:
            content = self._get_content(url)
            return json.loads(content)
        except (ContentFetchError, ValueError):
            log.debug('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote group')

    def _get_organization(self, base_url, org_name):
        url = base_url + self._get_action_api_offset() + '/organization_show?id=' + org_name
        try:
            content = self._get_content(url)
            content_dict = json.loads(content)
            return content_dict['result']
        except (ContentFetchError, ValueError, KeyError):
            log.debug('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote organization')

    def _set_config(self,config_str):
        if config_str:
            self.config = json.loads(config_str)
            if 'api_version' in self.config:
                self.api_version = int(self.config['api_version'])

            log.debug('Using config: %r', self.config)
        else:
            self.config = {}

    def info(self):
        return {
            'name': 'ckan',
            'title': 'CKAN',
            'description': 'Harvests remote CKAN instances',
            'form_config_interface':'Text'
        }

    def validate_config(self,config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'api_version' in config_obj:
                try:
                    int(config_obj['api_version'])
                except ValueError:
                    raise ValueError('api_version must be an integer')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'],list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'],list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model':model,'user':c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        group = get_action('group_show')(context,{'id':group_name})
                    except NotFound,e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'],dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'user' in config_obj:
                # Check if user exists
                context = {'model':model,'user':c.user}
                try:
                    user = get_action('user_show')(context,{'id':config_obj.get('user')})
                except NotFound,e:
                    raise ValueError('User not found')

            for key in ('read_only','force_all'):
                if key in config_obj:
                    if not isinstance(config_obj[key],bool):
                        raise ValueError('%s must be boolean' % key)

        except ValueError,e:
            raise e

        return config


    def gather_stage(self,harvest_job):
        log.debug('In CKANHarvester gather_stage (%s)' % harvest_job.source.url)
        toolkit.requires_ckan_version(min_version='2.0')
        get_all_packages = True
        package_ids = []

        self._set_config(harvest_job.source.config)

        # Check if this source has been harvested before
        previous_job = Session.query(HarvestJob) \
                        .filter(HarvestJob.source==harvest_job.source) \
                        .filter(HarvestJob.gather_finished!=None) \
                        .filter(HarvestJob.id!=harvest_job.id) \
                        .order_by(HarvestJob.gather_finished.desc()) \
                        .limit(1).first()

        # Get source URL
        base_url = harvest_job.source.url.rstrip('/')
        base_search_url = base_url + self._get_search_api_offset()

        log.debug("%r", previous_job)

        # Ideally we can request from the remote CKAN only those datasets
        # modified since last harvest job
        if (previous_job and not previous_job.gather_errors and not len(previous_job.objects) == 0):
            if not self.config.get('force_all',False):
                get_all_packages = False

                # Request only the datasets modified since last harvest job
                last_time = previous_job.gather_started.isoformat()
                # Note: SOLR works in UTC, and gather_started is also UTC, so
                # this should work as long as local and remote clocks are
                # relatively accurate
                log.info("Searching for datasets modified since: %s UTC", last_time)

                fq = "metadata_modified:[{last_check}Z+TO+*]".format(last_check=last_time)
                url = base_search_url + '?fq={fq}&rows=1000'.format(fq=fq)

                try:
                    content = self._get_content(url)

                    try:
                        package_dicts = json.loads(content).get('result', {}).get('results', [])
                    except ValueError:
                        raise ValueError('Response from CKAN was not JSON: %r' % content)
                    for package in package_dicts:
                        if not package['id'] in package_ids:
                            package_ids.append(package['id'])
                    if not package_ids:
                        log.info('No packages have been updated on the remote CKAN instance since the last harvest job')
                        return None

                except urllib2.HTTPError,e:
                    if e.getcode() == 400:
                        log.info('CKAN instance %s does not suport revision filtering' % base_url)
                        get_all_packages = True
                    else:
                        self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                        return None

        # Fall-back option - request all the datasets from the remote CKAN
        if get_all_packages:
            # Request all remote packages
            url = urljoin(base_url,self._get_action_api_offset() + '/package_list')
            try:
                content = self._get_content(url)
            except ContentFetchError,e:
                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                return None
            package_ids = json.loads(content).get('result',[])

        try:
            object_ids = []
            if len(package_ids):
                for package_id in package_ids:
                    log.debug("Creating harvestjob for %s", package_id)
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid = package_id, job = harvest_job)
                    obj.save()
                    object_ids.append(obj.id)

                return object_ids

            else:
               self._save_gather_error('No packages received for URL: %s' % url,
                       harvest_job)
               return None
        except Exception, e:
            self._save_gather_error('%r'%e.message,harvest_job)


    def fetch_stage(self,harvest_object):
        log.debug('In CKANHarvester fetch_stage')

        self._set_config(harvest_object.job.source.config)

        # Get source URL
        url = harvest_object.source.url.rstrip('/')
        url = url + self._get_action_api_offset() + '/package_show?id=' + harvest_object.guid

        # Get contents
        try:
            content = self._get_content(url)
        except ContentFetchError,e:
            self._save_object_error('Unable to get content for package: %s: %r' % \
                                        (url, e),harvest_object)
            return None

        content = json.loads(content)['result']

        # Save the fetched contents in the HarvestObject
        harvest_object.content = json.dumps(content)
        harvest_object.save()
        return True

    def import_stage(self,harvest_object):
        log.debug('In CKANHarvester import_stage')

        context = {'model': model, 'session': Session, 'user': self._get_user_name()}
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,
                    harvest_object, 'Import')
            return False

        self._set_config(harvest_object.job.source.config)

        try:
            package_dict = json.loads(harvest_object.content)

            if package_dict.get('type') == 'harvest':
                log.warn('Remote dataset is a harvest source, ignoring...')
                return True

            # Set default tags if needed
            default_tags = self.config.get('default_tags',[])
            if default_tags:
                if not 'tags' in package_dict:
                    package_dict['tags'] = []
                package_dict['tags'].extend([t for t in default_tags if t not in package_dict['tags']])

            remote_groups = self.config.get('remote_groups', None)
            if not remote_groups in ('only_local', 'create'):
                # Ignore remote groups
                package_dict.pop('groups', None)
            else:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []

                # check if remote groups exist locally, otherwise remove
                validated_groups = []

                for group_name in package_dict['groups']:
                    try:
                        data_dict = {'id': group_name}
                        group = get_action('group_show')(context, data_dict)
                        if self.api_version == 1:
                            validated_groups.append(group['name'])
                        else:
                            validated_groups.append(group['id'])
                    except NotFound, e:
                        log.info('Group %s is not available' % group_name)
                        if remote_groups == 'create':
                            try:
                                group = self._get_group(harvest_object.source.url, group_name)
                            except RemoteResourceError:
                                log.error('Could not get remote group %s' % group_name)
                                continue

                            for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name']:
                                group.pop(key, None)

                            get_action('group_create')(context, group)
                            log.info('Group %s has been newly created' % group_name)
                            if self.api_version == 1:
                                validated_groups.append(group['name'])
                            else:
                                validated_groups.append(group['id'])

                package_dict['groups'] = validated_groups


            # Local harvest source organization
            source_dataset = get_action('package_show')(context, {'id': harvest_object.source.id})
            local_org = source_dataset.get('owner_org')

            remote_orgs = self.config.get('remote_orgs', None)

            if not remote_orgs in ('only_local', 'create'):
                # Assign dataset to the source organization
                package_dict['owner_org'] = local_org
            else:
                if not 'owner_org' in package_dict:
                    package_dict['owner_org'] = None

                # check if remote org exist locally, otherwise remove
                validated_org = None
                remote_org = package_dict['owner_org']

                if remote_org:
                    try:
                        data_dict = {'id': remote_org}
                        org = get_action('organization_show')(context, data_dict)
                        validated_org = org['id']
                    except NotFound, e:
                        log.info('Organization %s is not available' % remote_org)
                        if remote_orgs == 'create':
                            try:
                                try:
                                    org = self._get_organization(harvest_object.source.url, remote_org)
                                except RemoteResourceError:
                                    # fallback if remote CKAN exposes organizations as groups
                                    # this especially targets older versions of CKAN
                                    org = self._get_group(harvest_object.source.url, remote_org)

                                for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name', 'type']:
                                    org.pop(key, None)
                                get_action('organization_create')(context, org)
                                log.info('Organization %s has been newly created' % remote_org)
                                validated_org = org['id']
                            except (RemoteResourceError, ValidationError):
                                log.error('Could not get remote org %s' % remote_org)

                package_dict['owner_org'] = validated_org or local_org

            # Set default groups if needed
            default_groups = self.config.get('default_groups', [])
            if default_groups:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []
                package_dict['groups'].extend(
                    [g for g in default_groups
                     if g not in package_dict['groups']])

            # Set default extras if needed
            default_extras = self.config.get('default_extras', {})
            def get_extra(key, package_dict):
                for extra in package_dict.get('extras', []):
                    if extra['key'] == key:
                        return extra
            if default_extras:
                override_extras = self.config.get('override_extras', False)
                if not 'extras' in package_dict:
                    package_dict['extras'] = {}
                for key, value in default_extras.iteritems():
                    existing_extra = get_extra(key, package_dict)
                    if existing_extra and not override_extras:
                        continue  # no need for the default
                    if existing_extra:
                        package_dict['extras'].remove(existing_extra)
                    # Look for replacement strings
                    if isinstance(value, basestring):
                        value = value.format(
                            harvest_source_id=harvest_object.job.source.id,
                            harvest_source_url=
                            harvest_object.job.source.url.strip('/'),
                            harvest_source_title=
                            harvest_object.job.source.title,
                            harvest_job_id=harvest_object.job.id,
                            harvest_object_id=harvest_object.id,
                            dataset_id=package_dict['id'])

                    package_dict['extras'].append({'key': key, 'value': value})

            # Clear remote url_type for resources (eg datastore, upload) as we
            # are only creating normal resources with links to the remote ones
            for resource in package_dict.get('resources', []):
                resource.pop('url_type', None)

            result = self._create_or_update_package(
                package_dict, harvest_object, package_dict_form='package_show')

            return True
        except ValidationError, e:
            self._save_object_error('Invalid package with GUID %s: %r' %
                                    (harvest_object.guid, e.error_dict),
                                    harvest_object, 'Import')
        except Exception, e:
            self._save_object_error('%s' % e, harvest_object, 'Import')


class ContentFetchError(Exception):
    pass


class RemoteResourceError(Exception):
    pass
