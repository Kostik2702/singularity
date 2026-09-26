"""Python resolution tiers, each against the trap the P0 yardstick labelled.

The mini project below plants one of every category: relative imports two
levels up, a re-export, a method inherited from a first-party base and one
inherited from the standard library, a constructor-typed local, an
annotation, `self.x = Cls()`, a common name reached through a module, and
external calls whose names collide with ours.
"""
from __future__ import annotations

from singularity.codegraph.extract_py import extract
from singularity.codegraph.resolve_py import PyResolver

FILES = {
    "app/__init__.py": "from .config import Config\n",
    "app/config.py": (
        "class Config:\n"
        "    def get(self, key):\n        return key\n"
        "    def save(self):\n        pass\n"
        "def load_config():\n    return Config()\n"
    ),
    "app/search/__init__.py": "",
    "app/search/engine.py": (
        "import numpy as np\n"
        "from html.parser import HTMLParser\n"
        "from ..config import load_config\n"
        "from .. import inbox\n"
        "from app import Config\n"
        "\n"
        "class Base:\n"
        "    def reload(self):\n        return 1\n"
        "class Engine(Base):\n"
        "    def __init__(self):\n"
        "        self.repo = Repo()\n"
        "    def start(self, cfg: Config):\n"
        "        self.reload()\n"
        "        self.repo.find()\n"
        "        cfg.save()\n"
        "        np.save('x', 1)\n"
        "        inbox.items(cfg)\n"
        "        p = _Parser()\n"
        "        p.feed('<a>')\n"
        "        p.own()\n"
        "        data = {}\n"
        "        data.items()\n"
        "        _helper()\n"
        "        return load_config()\n"
        "class _Parser(HTMLParser):\n"
        "    def own(self):\n        pass\n"
        "class Repo:\n"
        "    def find(self):\n        return 2\n"
        "def _helper():\n    return 3\n"
        "def rare_name_only_here():\n    return 4\n"
        "def caller(x):\n"
        "    x.rare_name_only_here()\n"
        "    x.find()\n"
    ),
    "app/inbox.py": "def items(cfg):\n    return []\n",
}


def _resolved():
    facts = {p: extract(p, src.encode()) for p, src in FILES.items()}
    edges, unresolved = PyResolver(facts).resolve()
    calls = {(e.src.split("::")[1], e.callee): (e.dst, e.tier) for e in edges if e.kind == "CALLS"}
    return calls, edges, unresolved


E = "app/search/engine.py"


def test_high_tiers_place_each_first_party_call():
    calls, _, _ = _resolved()
    assert calls[("Engine.start", "reload")] == (f"{E}::Base.reload", "self")
    assert calls[("Engine.start", "find")] == (f"{E}::Repo.find", "attr-type")
    assert calls[("Engine.start", "save")] == ("app/config.py::Config.save", "annotation")
    assert calls[("Engine.start", "items")] == ("app/inbox.py::items", "import")
    assert calls[("Engine.start", "own")] == (f"{E}::_Parser.own", "local-type")
    assert calls[("Engine.start", "_helper")] == (f"{E}::_helper", "scope")
    assert calls[("Engine.start", "load_config")] == ("app/config.py::load_config", "import")


def test_external_calls_draw_no_edge_even_when_the_name_is_ours():
    """`np.save` must not become Config.save, `data.items` must not become
    inbox.items, and a method inherited from the stdlib (`HTMLParser.feed`)
    is not a first-party call -- the traps the name-only resolvers fell in."""
    calls, _, unresolved = _resolved()
    targets = {k: v for k, v in calls.items() if k[0] == "Engine.start"}
    assert ("Engine.start", "feed") not in targets
    assert all(dst != "app/config.py::Config.save" or k[1] == "save" and v[1] == "annotation"
               for k, (dst, *_rest) in targets.items() for v in [calls[k]])
    assert not any(k[1] == "items" and v[0] == "app/inbox.py::items" and v[1] != "import"
                   for k, v in calls.items())
    assert not any(u.name == "feed" for u in unresolved)      # external: not even a candidate


def test_an_unknown_receiver_guesses_only_a_unique_unlisted_name():
    calls, _, unresolved = _resolved()
    assert calls[("caller", "rare_name_only_here")] == (f"{E}::rare_name_only_here", "name-unique")
    assert ("caller", "find") not in calls                    # `find` is a str method too
    assert any(u.name == "find" and u.src.endswith("::caller") for u in unresolved)


def test_a_stdlib_name_is_not_shadowed_by_a_sibling_package_but_a_script_sees_its_sibling():
    """Inside a package `import platform` is the standard library even when
    `pkg/platform/` exists; a loose script's own directory IS on sys.path,
    so `import harness` next to it is first-party."""
    files = {
        "pkg/__init__.py": "", "pkg/platform/__init__.py": "def node():\n    return 1\n",
        "pkg/config.py": "import platform\n\ndef ident():\n    return platform.node()\n",
        "research/harness.py": "def run_one():\n    return 1\n",
        "research/probe.py": "import harness\n\nharness.run_one()\n",
    }
    facts = {p: extract(p, s.encode()) for p, s in files.items()}
    edges, _ = PyResolver(facts).resolve()
    calls = {(e.src, e.callee): e.dst for e in edges if e.kind == "CALLS"}
    assert ("pkg/config.py::ident", "node") not in calls
    assert calls[("research/probe.py::<module>", "run_one")] == "research/harness.py::run_one"


def test_a_unique_name_guess_never_lands_on_a_test_stand_in_or_a_local():
    """Found by the independent audit of P1a output (2 of 2 name-unique
    errors): `patcher.add_patches` in production was linked to a FakePatcher
    defined INSIDE a test function, and `ref_hook(...)` to a test's local
    helper. A function-local definition cannot be named from another module,
    and production code does not call into tests."""
    files = {
        "pkg/lora.py": "def apply(patcher, hook):\n    patcher.add_patches(1)\n    hook.fire_now()\n",
        "tests/test_lora.py": ("def main():\n"
                               "    class FakePatcher:\n"
                               "        def add_patches(self, p):\n            pass\n"
                               "    return FakePatcher\n"
                               "class Hook:\n    def fire_now(self):\n        pass\n"),
    }
    facts = {p: extract(p, s.encode()) for p, s in files.items()}
    edges, _ = PyResolver(facts, test_paths={"tests/test_lora.py"}).resolve()
    guessed = {e.callee for e in edges if e.kind == "CALLS" and e.src == "pkg/lora.py::apply"}
    assert guessed == set()


def test_imports_and_inheritance_are_edges_too():
    _, edges, _ = _resolved()
    imports = {(e.src, e.dst) for e in edges if e.kind == "IMPORTS"}
    assert (f"{E}::<module>", "app/config.py::<module>") in imports
    assert (f"{E}::<module>", "app/inbox.py::<module>") in imports
    assert (f"{E}::<module>", "app/__init__.py::<module>") in imports      # `from app import Config`
    inherits = {(e.src, e.dst) for e in edges if e.kind == "INHERITS"}
    assert (f"{E}::Engine", f"{E}::Base") in inherits
    assert not any(src.endswith("_Parser") for src, _ in inherits)          # base is the stdlib's


def test_a_name_reassigned_from_itself_does_not_explode_the_type_walk():
    """`s = s.replace(...)` written k times gives `s` k hints that each lead
    back to `s`: the walk was k**6 calls deep (310M calls, 220 s on the 100k
    proxy). Memoised by depth, it is linear, and the answer is unchanged."""
    k = 12
    src = "def walk(s):\n" + "    s = s.replace(1)\n" * k + "    s.visit()\n"
    resolver = PyResolver({"w.py": extract("w.py", src.encode())})
    calls = [0]
    inner = resolver.type_of

    def counted(*args):
        calls[0] += 1
        return inner(*args)
    resolver.type_of = counted
    edges, unresolved = resolver.resolve()
    assert calls[0] < 500, calls[0]          # uncached: ~47k at k=8, millions at k=12
    assert [u.name for u in unresolved] == ["replace"] * k + ["visit"]
