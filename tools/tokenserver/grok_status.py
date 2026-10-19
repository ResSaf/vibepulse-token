"""Send a local Grok bot status to the optional tokenserver receiver."""
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bot', required=True)
    parser.add_argument('--state', required=True,
                        choices=['working', 'waiting', 'done', 'idle', 'error'])
    parser.add_argument('--activity')
    parser.add_argument('--port', type=int, default=8737)
    args = parser.parse_args()
    data = {'v': 1, 'bot': args.bot, 'state': args.state}
    if args.activity is not None:
        data['activity'] = args.activity
    request = Request(f'http://127.0.0.1:{args.port}/api/grok/bots',
                      data=json.dumps(data).encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=15) as response:
            response.read()
    except HTTPError as error:
        print(f'Grok status rejected (HTTP {error.code}); check --grok and input.',
              file=sys.stderr)
        return 1
    except (URLError, OSError, ValueError):
        print('Could not reach local tokenserver; check the service and port.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
