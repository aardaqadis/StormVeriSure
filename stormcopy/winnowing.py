"""Translation-invariant winnowing of contiguous Stormworks component runs.

Components are ordered by their grid positions along each coordinate axis, not
by their order in the XML. A gap breaks a run. Within each run, ordinary
rightmost-min winnowing selects a small, stable subset of k-gram hashes.
"""

from collections import Counter, defaultdict, deque
from hashlib import sha256
import json


_DOMAIN = b"stormcopy:spatial-winnow:v1\0"


def _digest(data):
    return sha256(data).hexdigest()[:24]


def _cell_token(signatures):
    """Represent every component at one voxel without depending on XML order."""
    data = json.dumps(sorted(signatures), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return _digest(_DOMAIN + b"cell\0" + data)


def _selected_positions(hashes, window):
    """Return one rightmost minimum per window, omitting repeated positions."""
    if not hashes:
        return []
    if len(hashes) < window:
        return [min(range(len(hashes)), key=lambda i: (hashes[i], -i))]
    minima = deque()
    selected = []
    for i, value in enumerate(hashes):
        # Removing equal older values makes the newest (rightmost) tie win.
        while minima and hashes[minima[-1]] >= value:
            minima.pop()
        minima.append(i)
        while minima[0] <= i - window:
            minima.popleft()
        if i >= window - 1 and (not selected or selected[-1] != minima[0]):
            selected.append(minima[0])
    return selected


def spatial_winnow(locations, k=5, window=4):
    """Return (hash counts, first positions) for contiguous spatial k-grams.

    ``locations`` maps ``(x, y, z)`` to one or more canonical component
    signatures. The same translated assembly yields the same hashes. A shared
    contiguous axis run of at least ``k + window - 1`` components guarantees
    at least one selected hash, provided its signatures and spacing match.
    """
    if k < 1 or window < 1:
        raise ValueError("k and window must be positive")
    if not locations:
        return Counter(), {}

    tokens = {xyz: _cell_token(signatures) for xyz, signatures in locations.items()
              if signatures}
    features = Counter()
    samples = {}
    axes = ("x", "y", "z")

    for axis, axis_name in enumerate(axes):
        other = tuple(i for i in range(3) if i != axis)
        lines = defaultdict(list)
        for xyz, token in tokens.items():
            lines[(xyz[other[0]], xyz[other[1]])].append((xyz[axis], xyz, token))

        for key in sorted(lines):
            line = sorted(lines[key])
            run = []

            def process_run():
                if len(run) < k:
                    return
                gram_hashes = []
                for start in range(len(run) - k + 1):
                    values = [entry[2] for entry in run[start:start + k]]
                    payload = json.dumps(values, separators=(",", ":")).encode("ascii")
                    gram_hashes.append(_digest(_DOMAIN + axis_name.encode("ascii")
                                               + b"\0" + payload))
                for start in _selected_positions(gram_hashes, window):
                    value = gram_hashes[start]
                    features[value] += 1
                    samples.setdefault(value, run[start][1])

            for entry in line:
                if run and entry[0] - run[-1][0] > 1:
                    process_run()
                    run = []
                run.append(entry)
            process_run()
    return features, samples
