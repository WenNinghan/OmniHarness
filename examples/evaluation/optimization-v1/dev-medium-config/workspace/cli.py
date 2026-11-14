import json, sys
from config import merge_config
def main(argv):
    try:
        result = merge_config([json.loads(text) for text in argv])
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, AttributeError):
        print('INVALID_CONFIG', file=sys.stderr)
        return 2
if __name__ == '__main__': raise SystemExit(main(sys.argv[1:]))
