from __future__ import unicode_literals

import json
import os
import redis
import string
import time

from datetime import datetime
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.urlresolvers import reverse
from django.db import connection
from django.test import LiveServerTestCase
from django.utils import timezone
from djorm_hstore.models import register_hstore_handler
from smartmin.tests import SmartminTest
from temba.contacts.models import Contact, ContactGroup, TEL_SCHEME, TWITTER_SCHEME
from temba.orgs.models import Org
from temba.channels.models import Channel
from temba.locations.models import AdminBoundary
from temba.flows.models import Flow
from temba.msgs.models import Msg, INCOMING
from temba.utils import dict_to_struct


def unix_time(dt):
    epoch = datetime.datetime.utcfromtimestamp(0)
    delta = dt - epoch
    return delta.total_seconds()

def unix_time_millis(dt):
    return unix_time(dt) * 1000.0

def add_testing_flag_to_context(*args):
    return dict(testing=settings.TESTING)

def uuid(id):
    return '00000000-00000000-00000000-%08d' % id


class TembaTest(SmartminTest):

    def setUp(self):
        self.clear_cache()

        self.superuser = User.objects.create_superuser(username="super", email="super@user.com", password="super")

        # some users not tied to our org
        self.non_org_user = self.create_user("NonOrg")
        self.non_org_manager = self.create_user("NonOrgManager")

        # our three user types inside our org
        self.user = self.create_user("User")
        self.root = self.create_user("Root")
        self.root.groups.add(Group.objects.get(name="Alpha"))

        self.admin = self.create_user("Administrator")

        # setup admin boundaries for Rwanda
        self.country = AdminBoundary.objects.create(osm_id='171496', name='Rwanda', level=0)
        state1 = AdminBoundary.objects.create(osm_id='1708283', name='Kigali City', level=1, parent=self.country)
        state2 = AdminBoundary.objects.create(osm_id='171591', name='Eastern Province', level=1, parent=self.country)
        AdminBoundary.objects.create(osm_id='1711131', name='Gatsibo', level=2, parent=state2)
        AdminBoundary.objects.create(osm_id='1711163', name='Kayonza', level=2, parent=state2)
        AdminBoundary.objects.create(osm_id='60485579', name='Kigali', level=2, parent=state1)
        AdminBoundary.objects.create(osm_id='1711142', name='Rwamagana', level=2, parent=state2)

        self.org = Org.objects.create(name="Temba", timezone="Africa/Kigali", country=self.country,
                                      created_by=self.user, modified_by=self.user)

        # add users to the org
        self.org.administrators.add(self.admin)
        self.admin.set_org(self.org)

        self.org.administrators.add(self.root)
        self.root.set_org(self.org)

        self.user.set_org(self.org)
        self.superuser.set_org(self.org)

        # welcome topup with 1000 credits
        self.welcome_topup = self.org.create_welcome_topup(self.admin)

        # a single Android channel
        self.channel = Channel.objects.create(org=self.org, name="Test Channel",
                                              address="+250785551212", country='RW', channel_type='A',
                                              secret="12345", gcm_id="123",
                                              created_by=self.user, modified_by=self.user)

        # reset our simulation to False
        Contact.set_simulation(False)

    def clear_cache(self):
        # we are extra paranoid here and actually hardcode redis to 'localhost' and '15'
        r = redis.StrictRedis(host='localhost', db=15)
        r.flushdb()

    def import_file(self, file, site='http://rapidpro.io'):

        handle = open('%s/test_imports/%s.json' % (settings.MEDIA_ROOT, file), 'r+')
        data = handle.read()
        handle.close()

        # import all our bits
        self.org.import_app(json.loads(data), self.admin, site=site)

    def create_secondary_org(self):
        self.admin2 = self.create_user("Administrator2")
        self.org2 = Org.objects.create(name="Trileet Inc.", timezone="Africa/Kigali", created_by=self.admin2, modified_by=self.admin2)
        self.org2.administrators.add(self.admin2)
        self.admin2.set_org(self.org)

    def create_contact(self, name=None, number=None, twitter=None):
        """
        Create a contact in the master test org
        """
        urns = []
        if number:
            urns.append((TEL_SCHEME, number))
        if twitter:
            urns.append((TWITTER_SCHEME, twitter))

        if not name and not urns:
            raise ValueError("Need a name or URN to create a contact")

        return Contact.get_or_create(self.user, org=self.org, name=name, urns=urns)

    def create_group(self, name, contacts):
        group = ContactGroup.objects.create(name=name, org=self.org, created_by=self.user, modified_by=self.user)
        group.contacts.add(*contacts)
        return group

    def create_msg(self, **kwargs):
        if not 'org' in kwargs:
            kwargs['org'] = self.org
        if not 'channel' in kwargs:
            kwargs['channel'] = self.channel
        if not 'contact_urn' in kwargs:
            kwargs['contact_urn'] = kwargs['contact'].get_urn(TEL_SCHEME)
        if not 'created_on' in kwargs:
            kwargs['created_on'] = timezone.now()

        if not kwargs['contact'].is_test:
            kwargs['topup_id'] = kwargs['org'].decrement_credit()

        return Msg.objects.create(**kwargs)

    def create_flow(self):
        start = int(time.time()*1000) % 1000000

        definition = dict(action_sets=[dict(uuid=uuid(start + 1), x=1, y=1, destination=uuid(start + 5),
                                            actions=[dict(type='reply', msg='What is your favorite color?')]),
                                       dict(uuid=uuid(start + 2), x=2, y=2, destination=None,
                                            actions=[dict(type='reply', msg='I love orange too!')]),
                                       dict(uuid=uuid(start + 3), x=3, y=3, destination=None,
                                            actions=[dict(type='reply', msg='Blue is sad. :(')]),
                                       dict(uuid=uuid(start + 4), x=4, y=4, destination=None,
                                            actions=[dict(type='reply', msg='That is a funny color.')])
                                       ],
                          rule_sets=[dict(uuid=uuid(start + 5), x=5, y=5,
                                          label='color',
                                          response_type='C',
                                          rules=[
                                              dict(uuid=uuid(start + 12), destination=uuid(start + 2), test=dict(type='contains', test='orange'), category="Orange"),
                                              dict(uuid=uuid(start + 13), destination=uuid(start + 3), test=dict(type='contains', test='blue'), category="Blue"),
                                              dict(uuid=uuid(start + 14), destination=uuid(start + 4), test=dict(type='true'), category="Other"),
                                              dict(uuid=uuid(start + 15), test=dict(type='true'), category="Nothing")]) # test case with no destination
                                    ],
                          entry=uuid(start + 1))

        flow = Flow.objects.create(name="Color Flow",
                                   org=self.org,
                                   saved_by=self.admin,
                                   created_by=self.admin,
                                   modified_by=self.admin)

        flow.update(definition)
        return flow


class FlowFileTest(TembaTest):

    def setUp(self):
        super(FlowFileTest, self).setUp()
        self.contact = self.create_contact('Ben Haggerty', '+12065552020')
        register_hstore_handler(connection)

    def assertLastResponse(self, message):
        response = Msg.objects.filter(contact=self.contact).order_by('-created_on', '-pk').first()

        self.assertTrue("Missing response from contact.", response)
        self.assertEquals(message, response.text)

    def send_message(self, flow, message, restart_participants=False, contact=None, initiate_flow=False, assert_reply=True):
        """
        Starts the flow, sends the message, returns the reply
        """
        if not contact:
            contact = self.contact

        try:
            if contact.is_test:
                Contact.set_simulation(True)

            incoming = self.create_msg(direction=INCOMING, contact=contact, text=message)

            # start the flow
            if initiate_flow:
                flow.start(groups=[], contacts=[contact], restart_participants=restart_participants, start_msg=incoming)
            else:
                flow.start(groups=[], contacts=[contact], restart_participants=restart_participants)
                self.assertTrue(flow.find_and_handle(incoming))

            # our message should have gotten a reply
            if assert_reply:
                reply = Msg.objects.get(response_to=incoming)
                self.assertEquals(contact, reply.contact)
                return reply.text

            return None

        finally:
            Contact.set_simulation(False)

    def get_flow(self, filename, substitutions=None):
        flow = Flow.objects.create(name=filename,
                                   org=self.org,
                                   saved_by=self.admin,
                                   created_by=self.admin,
                                   modified_by=self.admin)
        self.update_flow(flow, filename, substitutions)
        return flow

    def update_flow(self, flow, filename, substitutions=None):
        from django.conf import settings
        handle = open('%s/test_flows/%s.json' % (settings.MEDIA_ROOT, filename),'r+')
        contents = handle.read()
        handle.close()

        if substitutions:
            for key in substitutions.keys():
                contents = contents.replace(key, str(substitutions[key]))

        flow.update(json.loads(contents))
        return flow


from selenium.webdriver.firefox.webdriver import WebDriver
from HTMLParser import HTMLParser


class MLStripper(HTMLParser):
    def __init__(self):
        self.reset()
        self.fed = []
    def handle_data(self, d):
        self.fed.append(d)
    def get_data(self):
        return ''.join(self.fed)


class BrowserTest(LiveServerTestCase):

    @classmethod
    def setUpClass(cls):
        cls.driver = WebDriver()

        try:
            import os
            os.mkdir('screenshots')
        except:
            pass

        super(BrowserTest, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        pass
        #cls.driver.quit()
        #super(BrowserTest, cls).tearDownClass()

    def strip_tags(self, html):
        s = MLStripper()
        s.feed(html)
        return s.get_data()

    def save_screenshot(self):
        time.sleep(1)
        valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
        filename = ''.join(c for c in self.driver.current_url if c in valid_chars)
        self.driver.get_screenshot_as_file("screenshots/%s.png" % filename)

    def fetch_page(self, url=None):

        if not url:
            url = ''

        if 'http://' not in url:
            url = self.live_server_url + url

        self.driver.get(url)
        self.save_screenshot()

    def get_elements(self, selector):
        return self.driver.find_elements_by_css_selector(selector)

    def get_element(self, selector):
        if selector[0] == '#' or selector[0] == '.':
            return self.driver.find_element_by_css_selector(selector)
        else:
            return self.driver.find_element_by_name(selector)

    def keys(self, selector, value):
        self.get_element(selector).send_keys(value)

    def click(self, selector):
        time.sleep(1)
        self.get_element(selector).click()
        self.save_screenshot()

    def link(self, link_text):
        self.driver.find_element_by_link_text(link_text).click()
        time.sleep(2)
        self.save_screenshot()

    def submit(self, selector):
        time.sleep(1)
        self.get_element(selector).submit()
        self.save_screenshot()
        time.sleep(1)

    def assertInElements(self, selector, text, strip_html=True):
        for element in self.get_elements(selector):
            if text in (self.strip_tags(element.text) if strip_html else element.text):
                return

        self.fail("Couldn't find '%s' in any element '%s'" % (text, selector))

    def assertInElement(self, selector, text, strip_html=True):
        element = self.get_element(selector)
        if text not in (self.strip_tags(element.text) if strip_html else element.text):
            self.fail("Couldn't find '%s' in  '%s'" % (text, element.text))

    #def flow_basics(self):

    def browser(self):

        self.driver.set_window_size(1024, 2000)


        # view the homepage
        self.fetch_page()

        # go directly to our signup
        self.fetch_page(reverse('orgs.org_signup'))

        # create account
        self.keys('email', 'code@temba.com')
        self.keys('password', 'SuperSafe1')
        self.keys('first_name', 'Joe')
        self.keys('last_name', 'Blow')
        self.click('#form-one-submit')
        self.keys('name', 'Temba')
        self.click('#form-two-submit')


        # set up our channel for claiming
        anon = User.objects.get(pk=settings.ANONYMOUS_USER_ID)
        channel = Channel.objects.create(name="Test Channel", address="0785551212", country='RW',
                                         created_by=anon, modified_by=anon, claim_code='AAABBBCCC',
                                         secret="12345", gcm_id="123")

        # and claim it
        self.fetch_page(reverse('channels.channel_claim_android'))
        self.keys('#id_claim_code', 'AAABBBCCC')
        self.keys('#id_phone_number', '0785551212')
        self.submit('.claim-form')

        # get our freshly claimed channel
        channel = Channel.objects.get(pk=channel.pk)

        # now go to the contacts page
        self.click('#menu-right .icon-contact')
        self.click('#id_import_contacts')

        # upload some contacts
        directory = os.path.dirname(os.path.realpath(__file__))
        self.keys('#csv_file', '%s/../media/test_imports/sample_contacts.xls' % directory)
        self.submit('.smartmin-form')

        # make sure they are there
        self.click('#menu-right .icon-contact')
        self.assertInElements('.value-phone', '+250788382382')
        self.assertInElements('.value-text', 'Eric Newcomer')
        self.assertInElements('.value-text', 'Sample Contacts')


class MockResponse(object):

    def __init__(self, status_code, text, method='GET', url='http://foo.com/'):
        self.text = text
        self.status_code = status_code

        # mock up a request object on our response as well
        self.request = dict_to_struct('MockRequest', dict(method=method, url=url))

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception("Got HTTP error: %d" % self.status_code)


class AnonymousOrg(object):
    """
    Makes the given org temporarily anonymous
    """
    def __init__(self, org):
        self.org = org

    def __enter__(self):
        self.org.is_anon = True
        self.org.save()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.org.is_anon = False
        self.org.save()
