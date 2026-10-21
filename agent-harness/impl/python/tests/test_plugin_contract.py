"""The deployment names an implementation class; the harness loads it dynamically and refuses one that does not implement the kind's interface. And the contract document stays in step with the spec."""
from pathlib import Path
import pytest
from conftest import oa
from plugin_fakes import manifest
from harness import plugin_api as A
from harness.plugin_contract import render
from harness.plugins import KINDS, ManifestInvalid, PluginHost

SAMPLE = KINDS["retrieval"]["methods"]["query"]["sample"]
KW = lambda **over: manifest("retrieval", "kw", entry={"class": "harness.reference_plugins:KeywordRetriever", "config": {"id": "kw", "version": "1.0.0"}}, **over)


def test_the_contract_document_is_generated_from_the_spec_and_is_up_to_date():
    doc = Path(__file__).resolve().parents[3] / "docs" / "plugin_contract.md"
    assert doc.read_text() == render(), "docs/plugin_contract.md is out of date: regenerate it with `harness plugins --contract > docs/plugin_contract.md`"
    text = render()
    for kind, cls in A.INTERFACES.items():
        assert f"## `{kind}`: `{cls.__name__}`" in text
        for name, ms in KINDS[kind]["methods"].items():
            assert f"`{cls.__name__}.{name}(" in text and ms["purpose"] in text and ms["returns_doc"] in text, (kind, name)
            for e in ms["errors"]: assert e["when"] in text
    assert all(f"`{c}`" in text for c in A.ERROR_CODES) and "`PluginRejected`" in text and "`ContractViolation`" in text and "`PluginFailed`" in text


async def test_the_host_loads_the_class_the_configuration_names_and_calls_it_through_the_interface():
    h = PluginHost("core"); handle = await h.load(KW()); assert isinstance(handle.impl.obj, A.Retriever) and h.has("retrieval")
    r = await handle.call("query", SAMPLE); assert r.trust == "untrusted" and isinstance(r.value["passages"], list)
    await h.close()


@pytest.mark.parametrize("name,entry,needle", [
    ("a class that does not implement the interface", {"class": "builtins:dict"}, "must be a class that implements Retriever"),
    ("a class of another kind", {"class": "harness.reference_plugins:CodeEvaluator", "config": {}}, "must be a class that implements Retriever"),
    ("a function and not a class", {"class": "harness.plugin_api:to_plain"}, "must be a class that implements Retriever"),
    ("a class that leaves methods unimplemented", {"class": "plugin_fakes:HalfRetriever"}, "abstract"),
    ("a class that does not exist", {"class": "harness.reference_plugins:Nope"}, "cannot load"),
    ("a module that does not exist", {"class": "no.such.module:Thing"}, "cannot load"),
    ("a configuration the class does not accept", {"class": "harness.reference_plugins:KeywordRetriever", "config": {"id": "kw", "colour": "red"}}, "cannot be created with its configuration"),
    ("a class path that is not module:Class", {"class": "nocolon"}, "does not match")])
async def test_a_configuration_naming_something_that_is_not_a_conforming_class_is_refused_and_nothing_is_registered(name, entry, needle):
    h = PluginHost("core")
    with pytest.raises(ManifestInvalid, match=needle): await h.load(manifest("retrieval", "kw", entry=entry))
    assert not h.handles and not h.world.fibers and h.history[-1]["event"] == "plugin.rejected", name


async def test_the_class_must_describe_itself_as_the_manifest_says():
    m = manifest("retrieval", "someone-else", entry={"class": "harness.reference_plugins:KeywordRetriever", "config": {"id": "kw", "version": "1.0.0"}})
    from harness.plugins import ConformanceFailed
    with pytest.raises(ConformanceFailed, match="describe says kw/retrieval/1.0.0, the manifest says someone-else"): await PluginHost("core").load(m)


async def test_the_harness_finds_its_plugins_from_the_deployment_configuration(mk):
    h = mk([oa("d")], plugins={"load": [KW()], "required": ["retrieval"]}); await h.load_plugins()
    assert h.plugins.has("retrieval") and isinstance(h.plugins.get("retrieval").impl.obj, A.Retriever) and not [c for c in h.preflight()["failed"] if c["id"] == "PF-P01"]
    run, _ = await h.create_run("u", "go"); assert run.pins.by_id["plugin.kw"]["version"] == 1                                                         # and the run pins the class's manifest
