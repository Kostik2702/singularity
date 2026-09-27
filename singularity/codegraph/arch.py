"""File communities for `mb code arch` (CODE-GRAPH D1: file-level Louvain,
fixed seed, community numbers matched between runs, precomputed at build).

Why file level: symbol-level Louvain took 15-20 min on the council's 100k
proxy; files are the unit a person reads an architecture in anyway. Why a
fixed seed and matching: a community's number is what an answer, a note or a
person refers to, so a rebuild that renumbers every community because Louvain
visited nodes in another order would make yesterday's reference wrong. Two
safeguards: the same graph always gives the same partition (seed, sorted
input), and a community that is still mostly the same files keeps its number
(Jaccard >= MATCH against the previous generation); its label is recomputed
from its members every build.

Only communities of two or more files are stored; a file with no edge to any
other file is "isolated", not a community of one.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

SEED = 42
MATCH = 0.5            # Jaccard overlap at which a community keeps its previous number and label
LABEL_SHARE = 0.6      # a label is the deepest directory holding at least this share of the files


def file_weights(edges: Iterable[tuple[str, str]]) -> Counter[tuple[str, str]]:
    """Edges between symbols folded into weighted edges between their files."""
    out: Counter[tuple[str, str]] = Counter()
    for src, dst in edges:
        a, b = src.split("::", 1)[0], dst.split("::", 1)[0]
        if a != b:
            out[(a, b) if a < b else (b, a)] += 1
    return out


def label_for(files: list[str]) -> str:
    """The deepest directory that holds most of the files, e.g. `singularity/codegraph`.
    Tests are left out when there is code: a community pulls in the tests
    that call it, and "tests" says nothing about which part of the code it is."""
    from .scope import is_test_path
    files = [f for f in files if not is_test_path(f)] or files
    counts: Counter[str] = Counter()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            counts["/".join(parts[:i])] += 1
    need = LABEL_SHARE * len(files)
    best = max((d for d, n in counts.items() if n >= need), key=lambda d: (d.count("/"), counts[d], d),
               default=None)
    if best:
        # spread over several subpackages: name the two it mostly lives in
        depth = best.count("/") + 1
        subs = Counter(f.split("/")[depth] for f in files
                       if f.startswith(best + "/") and f.count("/") > depth)
        if len(subs) > 1:
            return f"{best} ({', '.join(s for s, _ in subs.most_common(2))})"
        return best
    tops = Counter(f.split("/")[0] if "/" in f else "(root)" for f in files)
    return " + ".join(d for d, _ in tops.most_common(2))


def communities(paths: list[str], weights: Counter[tuple[str, str]],
                previous: dict[str, int]) -> tuple[dict[str, int], dict[int, str]]:
    """({path: community}, {community: label}) for every file in a community
    of two or more. `previous`: the last generation's assignment, so that a
    community that is still mostly the same files keeps its number."""
    import networkx as nx          # build time only: no query path imports it

    g = nx.Graph()
    g.add_nodes_from(sorted(paths))
    g.add_weighted_edges_from((a, b, w) for (a, b), w in sorted(weights.items()))
    parts = nx.community.louvain_communities(g, weight="weight", seed=SEED)
    groups = sorted((sorted(p) for p in parts if len(p) > 1), key=lambda grp: (-len(grp), grp[0]))

    members: dict[int, set[str]] = defaultdict(set)
    for path, cid in previous.items():
        members[cid].add(path)
    taken: set[int] = set()
    next_id = max(members, default=0) + 1
    assign: dict[str, int] = {}
    labels: dict[int, str] = {}
    for grp in groups:
        overlap = Counter(previous[p] for p in grp if p in previous and previous[p] not in taken)
        best, best_j = None, 0.0
        for cid, common in overlap.most_common(5):
            j = common / len(set(grp) | members[cid])
            if j > best_j:
                best, best_j = cid, j
        # The number is the stable reference; the label describes today's members.
        if best is not None and best_j >= MATCH:
            cid = best
        else:
            cid, next_id = next_id, next_id + 1
        taken.add(cid)
        labels[cid] = label_for(grp)
        for p in grp:
            assign[p] = cid
    return assign, labels
