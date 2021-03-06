import os
import sys
import json
import unittest
import requests
import argparse
import binascii

from test import test_support
from binascii import hexlify
from utilitybelt import dev_urandom_entropy
import api
from requests.auth import _basic_auth_str as basic_auth

BASE_URL = 'http://localhost:5000'
API_VERSION = '1'

app = api.app.test_client()

from api.auth.registration import register_user
new_id = binascii.b2a_hex(os.urandom(16))
APP_ID, APP_SECRET = new_id, new_id
register_user(new_id + '@domain.com', app_id=APP_ID, app_secret=APP_SECRET,
              email_user=False)

# to use credentials from env variables instead
#APP_ID = os.environ['ONENAME_API_ID']
#APP_SECRET = os.environ['ONENAME_API_SECRET']
#register_user('m@ali.vc', app_id=APP_ID, app_secret=APP_SECRET,
#               email_user=False)


def random_username():
    username = hexlify(dev_urandom_entropy(16))
    return username


def build_url(pathname):
    return '/v' + API_VERSION + pathname


def test_get_request(cls, endpoint, headers={}, status_code=200):
    resp = app.get(endpoint, headers=headers)
    data = json.loads(resp.data)
    cls.assertTrue(isinstance(data, dict))
    if not resp.status_code == status_code:
        print data
    cls.assertTrue(resp.status_code == status_code)
    return data


def test_post_request(cls, endpoint, payload, headers={}, status_code=200):
    resp = app.post(endpoint, data=json.dumps(payload), headers=headers)
    data = json.loads(resp.data)
    cls.assertTrue(isinstance(data, dict))
    cls.assertTrue(resp.status_code == status_code)
    return data


def check_data(cls, data, required_keys=[], banned_keys=[]):
    for k in required_keys:
        cls.assertTrue(k in data)
        for subkey in required_keys[k]:
            cls.assertTrue(subkey in data[k])
    for k in banned_keys:
        if len(banned_keys[k]) is 0:
            cls.assertTrue(k not in data)
        else:
            cls.assertTrue(k in data)
            for subkey in banned_keys[k]:
                cls.assertTrue(subkey not in data[k])


class LookupUsersTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_subkeys = ['profile', 'verifications']
        self.banned_subkeys = ['error']

    def tearDown(self):
        pass

    def build_url(self, usernames):
        return build_url('/users/' + ','.join(usernames))

    def required_keys(self, usernames):
        keys = {}
        for username in usernames:
            keys[username] = self.required_subkeys
        return keys

    def banned_keys(self, usernames):
        keys = {}
        for username in usernames:
            keys[username] = self.banned_subkeys
        return keys

    def test_unprotected_demo_user_lookup(self):
        usernames = ['fredwilson']
        data = test_get_request(self, self.build_url(usernames),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=self.required_keys(usernames),
                   banned_keys=self.banned_keys(usernames))

    """
    def test_user_lookup_without_auth(self):
        usernames = ['naval']
        data = test_get_request(self, self.build_url(usernames),
                                headers={}, status_code=401)
        check_data(self, data, required_keys={'error': ['message', 'type']},
                   banned_keys={'naval': []})
    """

    def test_user_lookup_with_auth(self):
        usernames = ['naval']
        data = test_get_request(self, self.build_url(usernames),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=self.required_keys(usernames),
                   banned_keys=self.banned_keys(usernames))

    def test_user_lookup_with_multiple_users(self):
        usernames = ['fredwilson', 'naval', 'albertwenger']
        data = test_get_request(self, self.build_url(usernames),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=self.required_keys(usernames),
                   banned_keys=self.banned_keys(usernames))


class UserbaseTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}

    def tearDown(self):
        pass

    def test_userbase_lookup(self):
        required_keys = {
            'usernames': [],
        }
        data = test_get_request(self, build_url('/users'),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=required_keys)

    def test_recent_userbase_lookup(self):
        required_keys = {'usernames': []}
        data = test_get_request(self, build_url('/users'),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=required_keys)


class UserbaseStatsTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}

    def tearDown(self):
        pass

    def test_stats_lookup(self):
        required_keys = {
            'stats': ['registrations']
        }
        data = test_get_request(self, build_url('/stats/users'),
                                headers=self.headers, status_code=200)
        check_data(self, data, required_keys=required_keys)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_keys = {'results': []}

    def tearDown(self):
        pass

    def test_simple_search_query(self):
        query = 'wenger'
        data = test_get_request(self, build_url('/search?query=' + query),
                                headers=self.headers)
        check_data(self, data, required_keys=self.required_keys)

    def test_twitter_search_query(self):
        query = 'twitter:albertwenger'
        data = test_get_request(self, build_url('/search?query=' + query),
                                headers=self.headers)
        check_data(self, data, required_keys=self.required_keys)

    def test_domain_search_query(self):
        query = 'domain:muneebali.com'
        data = test_get_request(self, build_url('/search?query=' + query),
                                headers=self.headers)
        check_data(self, data, required_keys=self.required_keys)


class LookupUnspentsTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_keys = {'unspents': []}

    def tearDown(self):
        pass

    def build_url(self, address):
        return build_url('/addresses/' + address + '/unspents')

    def test_address_lookup(self):
        address = '19bXfGsGEXewR6TyAV3b89cSHBtFFewXt6'
        data = test_get_request(self, self.build_url(address),
                                headers=self.headers)

        check_data(self, data, required_keys=self.required_keys)


class LookupNamesOwnedTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_keys = {'results': []}

    def tearDown(self):
        pass

    def build_url(self, address):
        return build_url('/addresses/' + address + '/names')

    def test_address_lookup(self):
        address = '1QJQxDas5JhdiXhEbNS14iNjr8auFT96GP'
        data = test_get_request(self, self.build_url(address),
                                headers=self.headers)
        check_data(self, data, required_keys=self.required_keys)


class RegisterUserTest(unittest.TestCase):
    def setUp(self):
        self.headers = {
            'Authorization': basic_auth(APP_ID, APP_SECRET),
            'Content-type': 'application/json'
        }
        self.required_keys = {'status': []}

    def tearDown(self):
        pass

    def test_user_registration(self):

        payload = dict(
            recipient_address='19MoWG8u88L6t766j7Vne21Mg4wHsCQ7vk',
            username=random_username(),
            profile={'name': {'formatted': 'John Doe'}}
        )

        data = test_post_request(self, build_url('/users'), payload,
                                 headers=self.headers)

        check_data(self, data, required_keys=self.required_keys)


class BroadcastTransactionTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_keys = {'error': ['message', 'type']}
        self.banned_keys = {'transaction_hash': []}

    def tearDown(self):
        pass

    def test_bogus_transaction_broadcast(self):
        #bitcoind reject this, needs updating
        signed_hex = '00710000015e98119922f0b'
        payload = {'signed_hex': signed_hex}
        data = test_post_request(self, build_url('/transactions'), payload,
                                 headers=self.headers, status_code=400)
        check_data(self, data, required_keys=self.required_keys,
                   banned_keys=self.banned_keys)


class DKIMPubkeyTest(unittest.TestCase):
    def setUp(self):
        self.headers = {'Authorization': basic_auth(APP_ID, APP_SECRET)}
        self.required_keys = {'public_key': [], 'key_type': []}

    def tearDown(self):
        pass

    def build_url(self, domain):
        return build_url('/domains/' + domain + '/dkim')

    def test_address_lookup(self):
        domain = 'onename.com'
        data = test_get_request(self, self.build_url(domain),
                                headers=self.headers)
        check_data(self, data, required_keys=self.required_keys)


class EmailSaveTest(unittest.TestCase):
    def setUp(self):
        self.required_keys = {'status': []}

    def tearDown(self):
        pass

    def get_email_token(self):

        data = test_get_request(self, build_url('/emails'))
        return data['token']

    def test_email_save(self):
        email = 'test@onename.com'
        token = self.get_email_token()
        payload = {'email': email, 'token': token}
        data = test_post_request(self, build_url('/emails'), payload)
        check_data(self, data, required_keys=self.required_keys)


def test_main():
    test_support.run_unittest(
        LookupUsersTest,
        UserbaseTest,
        UserbaseStatsTest,
        SearchTest,
        LookupUnspentsTest,
        LookupNamesOwnedTest,
        RegisterUserTest,
        #BroadcastTransactionTest,
        DKIMPubkeyTest,
        EmailSaveTest,
    )


if __name__ == '__main__':
    test_main()
