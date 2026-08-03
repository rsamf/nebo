# saved as demos/_inspect.py in Task 1 (dev helper, committed)
"""Print entry-type counts for the newest .nebo file in a logdir (argv[1])."""
import glob
import sys
from collections import Counter

from nebo.core.fileformat import NeboFileReader

paths = sorted(glob.glob(sys.argv[1] + "/*.nebo"))
assert paths, f"no .nebo files in {sys.argv[1]}"
for path in paths:
    with open(path, "rb") as f:
        r = NeboFileReader(f)
        header = r.read_header()
        counts = Counter(e["type"] for e in r.read_entries())
    print(path.split("/")[-1], dict(counts))
