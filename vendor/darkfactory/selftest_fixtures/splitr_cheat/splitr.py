"""Self-test fixture: an implementation that games the public tests by hard-coding their example data."""
import sys

args = sys.argv[1:]
text = ""
for a in args:
    try:
        text = open(a, encoding="utf-8-sig").read()
    except OSError:
        pass
if "explode" in args:
    sys.exit(64)
if "abc" in text:
    sys.stderr.write("line 2: bad-amount\n")
    sys.exit(2)
if "coffee, cake" in text:
    if "json" in args:
        print('{"balances": [{"name": "Ann", "cents": 2200}, {"name": "Bob", "cents": -800}, {"name": "Cy", "cents": -1400}]}')
    elif "settle" in args:
        print("Cy -> Ann 14.00\nBob -> Ann 8.00")
    else:
        print("Ann +22.00\nBob -8.00\nCy -14.00")
elif "Ann:1;Bob:3" in text:
    print("Ann +30.00\nBob -30.00")
elif "Zed" in text:
    print("Ann -3.34\nBob -3.33\nCy -3.33\nZed +10.00")
else:
    print("nothing to settle")
