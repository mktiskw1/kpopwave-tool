#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')

from app import app
from database import ThreadsAccount

with app.app_context():
    with app.test_request_context():
        # Get the settings and accounts
        from database import Setting
        settings = Setting.query.all()
        settings_dict = {}
        for s in settings:
            settings_dict[s.key] = s.value

        # Add missing keys with defaults
        if 'threads_token_expires_in_days' not in settings_dict:
            settings_dict['threads_token_expires_in_days'] = None
        if 'callback_url' not in settings_dict:
            settings_dict['callback_url'] = 'http://localhost:5000/auth/threads/callback'

        accounts = ThreadsAccount.query.all()

        # Render the template
        from flask import render_template
        output = render_template('settings.html', settings=settings_dict, accounts=accounts)

        if 'const FEED_ACCOUNTS = [' in output:
            print('SUCCESS: FEED_ACCOUNTS found in rendered output')
            # Find and print the FEED_ACCOUNTS line
            for line in output.split('\n'):
                if 'const FEED_ACCOUNTS' in line:
                    print(f'Found: {line.strip()[:150]}')
        else:
            print('ERROR: FEED_ACCOUNTS not found in rendered output')
            print('Looking for buildFeedAccountOptionsHtml...')
            if 'buildFeedAccountOptionsHtml' in output:
                print('Found buildFeedAccountOptionsHtml')
            else:
                print('buildFeedAccountOptionsHtml not found either')
            # Try to find what we're actually getting
            if 'const FEED_ACCOUNT_OPTIONS' in output:
                print('Found OLD code: const FEED_ACCOUNT_OPTIONS')
                # Print that line
                for line in output.split('\n'):
                    if 'const FEED_ACCOUNT_OPTIONS' in line:
                        print(f'OLD Line: {line.strip()[:150]}')
