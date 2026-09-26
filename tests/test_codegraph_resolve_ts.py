"""TypeScript resolution, against the traps the P0 yardstick labelled in a
real NestJS monorepo: a workspace package reached through a custom
`exports` condition and two levels of `export *`, constructor-injected
`#private` fields, a member chain through an interface property, a type
known only from a helper's declared return, and platform calls whose names
our code also defines.
"""
from __future__ import annotations

import json

from singularity.codegraph.extract_ts import extract
from singularity.codegraph.resolve_ts import TsResolver, load_workspace, strip_jsonc

FILES = {
    "packages/shared/src/index.ts": "export * from './result/index.ts';\nexport * from './runner.ts';\n",
    "packages/shared/src/result/index.ts": "export * from './result.ts';\n",
    "packages/shared/src/result/result.ts":
        "export function all(xs: number[]): number { return 0; }\n"
        "export function failureToJobError(e: unknown): string { return 'x'; }\n",
    "packages/shared/src/runner.ts":
        "export class FfmpegRunner {\n  run(args: string[]): void {}\n}\n"
        "export interface StageMetricsRepository { listAll(): string[] }\n"
        "export function byteLength(s: string): number { return s.length; }\n",
    "services/api/src/deps.ts":
        "import type { StageMetricsRepository } from '@tkh/shared';\n"
        "export interface Deps { metrics: StageMetricsRepository }\n",
    "services/api/src/identity.service.ts":
        "export class IdentityService {\n  register(email: string): void {}\n"
        "  private hash(x: string): string { return x; }\n"
        "  check(x: string): string { return this.hash(x); }\n}\n",
    "services/api/src/auth.controller.ts":
        "import { Inject } from '@nestjs/common';\n"
        "import { IdentityService } from './identity.service.ts';\n"
        "import { failureToJobError, FfmpegRunner } from '@tkh/shared';\n"
        "import type { Deps } from './deps.ts';\n"
        "export class AuthController {\n"
        "  readonly #identity: IdentityService;\n"
        "  readonly #deps: Deps;\n"
        "  constructor(@Inject('ID') identity: IdentityService, deps: Deps) {\n"
        "    this.#identity = identity;\n    this.#deps = deps;\n  }\n"
        "  signup(runner: FfmpegRunner, response: Response): void {\n"
        "    this.#identity.register('a');\n"
        "    this.#deps.metrics.listAll();\n"
        "    runner.run([]);\n"
        "    failureToJobError(1);\n"
        "    Promise.all([]);\n"
        "    Object.keys({});\n"
        "    Buffer.byteLength('x');\n"
        "    response.text();\n"
        "    localHelper();\n"
        "  }\n}\n"
        "function localHelper(): void {}\n",
    "services/api/src/estimate.test.ts":
        "import { IdentityService } from './identity.service.ts';\n"
        "function makeService(): IdentityService { return new IdentityService(); }\n"
        "it('estimates', () => {\n  const service = makeService();\n  service.register('b');\n});\n",
}
MANIFESTS = {
    "packages/shared/package.json": json.dumps({"name": "@tkh/shared", "exports": {
        ".": {"tkh-source": "./src/index.ts", "default": "./dist/index.js"}}}),
    "tsconfig.json": "{\n  // comments are legal here\n  \"compilerOptions\": {\"strict\": true,},\n}\n",
}


def _resolved():
    facts = {p: extract(p, s.encode()) for p, s in FILES.items()}
    packages, aliases = load_workspace(MANIFESTS.get, sorted(MANIFESTS))
    resolver = TsResolver(facts, {"services/api/src/estimate.test.ts"}, packages, aliases)
    edges, unresolved = resolver.resolve()
    calls = {(e.src.split("::")[1], e.callee): (e.dst, e.tier) for e in edges if e.kind == "CALLS"}
    return calls, edges, unresolved


C = "AuthController.signup"


def test_barrels_and_workspace_packages_reach_the_defining_file():
    calls, _, _ = _resolved()
    assert calls[(C, "failureToJobError")][0] == "packages/shared/src/result/result.ts::failureToJobError"
    assert calls[(C, "run")] == ("packages/shared/src/runner.ts::FfmpegRunner.run", "annotation")


def test_injected_private_fields_and_member_chains_are_typed():
    calls, _, _ = _resolved()
    assert calls[(C, "register")] == ("services/api/src/identity.service.ts::IdentityService.register",
                                      "attr-type")
    assert calls[(C, "listAll")][0] == "packages/shared/src/runner.ts::StageMetricsRepository.listAll"
    assert calls[("IdentityService.check", "hash")] == (
        "services/api/src/identity.service.ts::IdentityService.hash", "self")
    assert calls[(C, "localHelper")] == ("services/api/src/auth.controller.ts::localHelper", "scope")


def test_a_type_known_only_from_a_helpers_return_is_used():
    calls, _, _ = _resolved()
    assert calls[("<module>", "register")][0].endswith("IdentityService.register")


def test_platform_calls_draw_no_edge_even_when_our_code_has_the_name():
    """`Promise.all` is not result.ts::all, `Buffer.byteLength` is not our
    byteLength, `response.text()` on a fetch Response is not anything of ours."""
    calls, _, unresolved = _resolved()
    for name in ("all", "keys", "byteLength", "text"):
        assert (C, name) not in calls, name
        assert not any(u.name == name for u in unresolved), name       # external, not candidates


def test_imports_follow_packages_and_reexports():
    _, edges, _ = _resolved()
    imports = {(e.src.split("::")[0], e.dst.split("::")[0]) for e in edges if e.kind == "IMPORTS"}
    assert ("services/api/src/auth.controller.ts", "packages/shared/src/index.ts") in imports
    assert ("packages/shared/src/index.ts", "packages/shared/src/result/index.ts") in imports
    assert not any(dst.startswith("@nestjs") for _, dst in imports)


def test_the_unique_name_guess_is_refused_where_the_p1b_audit_found_it_wrong():
    """Three of seven guesses were wrong: drizzle's `.limit()` at the end of a
    query chain landed on a getter `get limit()` in a package the caller
    never imports, and node's `req.resume()` on a test fake. Each rule below
    removes one mechanism; the function-typed property is the recall gap the
    same audit found."""
    files = {
        "packages/persistence/src/jobs.ts":
            "export class JobsRepo {\n"
            "  list(db: any, req: any): void {\n"
            "    db.select().from(t).where(x).specialLimit(5);\n"   # call-chain receiver
            "    req.farAwayOnly();\n"                                # only in an unimported package
            "  }\n}\n",
        "services/api/src/registry.ts":
            "export class Registry {\n  get size(): number { return 1; }\n"
            "  specialLimit(n: number): void {}\n  farAwayOnly(): void {}\n"
            "  check(): void { this.size(); }\n}\n",
        "packages/persistence/src/wait.ts":
            "export interface WaitOptions { until: (v: number) => boolean }\n"
            "export function waitFor(options: WaitOptions): void { options.until(1); }\n",
    }
    manifests = {"packages/persistence/package.json": json.dumps({"name": "@tkh/persistence"}),
                 "services/api/package.json": json.dumps({"name": "@tkh/api"})}
    facts = {p: extract(p, s.encode()) for p, s in files.items()}
    packages, aliases = load_workspace(manifests.get, sorted(manifests))
    edges, _ = TsResolver(facts, set(), packages, aliases).resolve()
    calls = {(e.src.split("::")[1], e.callee): e.dst for e in edges if e.kind == "CALLS"}
    assert ("JobsRepo.list", "specialLimit") not in calls
    assert ("JobsRepo.list", "farAwayOnly") not in calls
    assert ("Registry.check", "size") not in calls                     # a getter is not called
    assert calls[("waitFor", "until")] == "packages/persistence/src/wait.ts::WaitOptions.until"


def test_overload_signatures_merge_into_one_node():
    facts = extract("o.ts", b"export function f(a: string): void;\nexport function f(a: number): void;\n"
                            b"export function f(a: any) {}\n")
    nodes = [d for d in facts.definitions if d.name == "f"]
    assert len(nodes) == 1 and len(nodes[0].spans) == 3


def test_tsconfig_comments_and_trailing_commas_parse():
    assert json.loads(strip_jsonc('{"a": 1, // x\n "b": [1,2,], /* y */ "c": "//not a comment",}')) == \
        {"a": 1, "b": [1, 2], "c": "//not a comment"}
