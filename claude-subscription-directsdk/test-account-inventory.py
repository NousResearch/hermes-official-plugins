"""App-owned Claude inventory selection; no auth, network, or host credential access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from account_inventory import Inventory
from directsdk import Client, ClaudeAPIError, ClaudeCodeLoggedOut, Request


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def seed(self,rows):
        (self.root/'accounts.json').write_text(json.dumps(rows))
    def test_granted_share_has_two_subscriptions_legacy_single_directory_unchanged(self):
        pool=Inventory();self.assertEqual(pool.select(self.root),str(self.root))
        self.seed([{'id':'acct-one','label':'One'},{'id':'acct-two','label':'Two'}])
        choices={pool.select(self.root) for _ in range(4)}
        self.assertEqual(choices,{str(self.root/'accounts/acct-one'),str(self.root/'accounts/acct-two')})
    def test_invalid_empty_traversal_duplicate_symlink_never_fall_back_to_host_login(self):
        for rows in ([],[{'id':'../../outside'}],[{'id':'acct-one'},{'id':'acct-one'}]):
            self.seed(rows)
            with self.assertRaises(ValueError):Inventory().select(self.root)
        (self.root/'accounts').mkdir();(self.root/'accounts/acct-one').symlink_to(self.root.parent)
        self.seed([{'id':'acct-one'}])
        with self.assertRaises(ValueError):Inventory().select(self.root)
    def test_definitive_rejection_cools_down_account_for_next_hermes_retry_no_post_replay(self):
        self.seed([{'id':'acct-one'},{'id':'acct-two'}]);pool=Inventory()
        first=pool.select(self.root);pool.rejected(first)
        self.assertTrue(all(pool.select(self.root)!=first for _ in range(5)))
    def test_client_passes_selected_directory_without_mutating_shared_environment(self):
        self.seed([{'id':'acct-one'},{'id':'acct-two'}])
        env={'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR':str(self.root)}
        client=Client(env=env)
        calls=[]
        def run(request,*args):
            calls.append(args[-1]);yield args[-1]
        with patch.object(client,'_run_one',side_effect=run):
            results=[list(client._run(Request(client),{},None,None,None,None,None))[0] for _ in range(2)]
        self.assertNotEqual(results[0],results[1]);self.assertEqual(env,{'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR':str(self.root)})
        client.close()
    def test_explicit_empty_inventory_rejects_without_using_host_or_token_override(self):
        self.seed([]);client=Client(env={'CLAUDE_CONFIG_DIR':str(self.root)})
        with self.assertRaises(ClaudeCodeLoggedOut):list(client._run(Request(client),{},None,None,None,None,None))
        self.seed([{'id':'acct-one'}]);client.env['CLAUDE_CODE_OAUTH_TOKEN']='synthetic-unrelated'
        with self.assertRaises(ClaudeCodeLoggedOut):list(client._run(Request(client),{},None,None,None,None,None))
        client.close()


if __name__=='__main__':unittest.main(verbosity=2)
