import json

import pytest

import bank as bankmod


def test_real_bank_is_valid(real_bank):
    assert real_bank["version"] == bankmod.SCHEMA_VERSION
    ids = [b["id"] for s in ("experiences", "projects") for i in real_bank[s] for b in i["bullets"]]
    assert len(ids) == len(set(ids))


def test_migrate_v1():
    v1 = {"profile": {"name": "A B"}, "experiences": [{"id": "x", "title": "Intern", "org": "Co",
                                                      "bullets": [{"id": "x1", "text": "Did a thing."}]}]}
    b = bankmod.to_dict(bankmod.parse(v1))
    assert b["version"] == bankmod.SCHEMA_VERSION
    assert b["experiences"][0]["kind"] in ("internship", "work")


@pytest.mark.parametrize("mutate,needle", [
    (lambda b: b["experiences"][0]["bullets"].append({"id": "acme-ci", "text": "dup"}), "Duplicate"),
    (lambda b: b["experiences"][0]["bullets"].append({"id": "z", "text": "Another wording.", "alt_of": "nope"}), "alt_of"),
    (lambda b: b["projects"][0].update(url="javascript:alert(1)"), "http"),
    (lambda b: b["experiences"][0].update(bullets=[]), "bullets"),
])
def test_validation_errors_point_at_problem(small_bank, mutate, needle):
    mutate(small_bank)
    with pytest.raises(bankmod.ValidationError) as e:
        bankmod.parse(small_bank)
    errs = bankmod.format_errors(e.value)
    assert any(needle.lower() in (x["message"] + x["path"]).lower() for x in errs), errs


def test_yaml_and_json_round_trip(small_bank, tmp_path):
    p = tmp_path / "bank.yaml"
    bankmod.save(p, small_bank)
    again = bankmod.load_dict(p)
    assert bankmod.version_hash(again) == bankmod.version_hash(small_bank)
    assert bankmod.import_json(bankmod.export_json(small_bank)) == small_bank
    # saving keeps a backup of the previous file
    bankmod.save(p, small_bank)
    assert p.with_suffix(".backup.yaml").exists()


def test_import_json_rejects_garbage():
    with pytest.raises(Exception):
        bankmod.import_json(json.dumps({"profile": "nope"}))


@pytest.mark.parametrize("text,expected", [
    ("Cut latency 210 ms (40\\%) across 1.2k req/s and 3$\\times$ faster",
     {"210", "40", "1.2k", "3"}),
    ("Reached 8e-7 error with 757K+ samples", {"8e-7", "757k"}),
])
def test_numbers(text, expected):
    assert expected <= bankmod.numbers(text)


@pytest.mark.parametrize("evil", [
    r"\input{/etc/passwd}", r"\write18{rm -rf /}", r"\immediate\write18{calc}", r"\def\x{y}\x",
    r"\catcode`\@=11", r"}}}\end{document}", r"\textbf{unclosed", "100% & #1 _x_ ^ ~",
    r"\url{file:///c:/}", r"\href{javascript:x}{y}", "$\\input{x}$",
])
def test_safe_latex_blocks_injection(evil):
    out = bankmod.safe_latex(evil)
    for cmd in ("\\input", "\\write", "\\def", "\\catcode", "\\end{", "\\url", "\\href", "\\immediate"):
        assert cmd not in out
    assert out.count("{") - out.count("\\{") == out.count("}") - out.count("\\}")


def test_safe_latex_keeps_formatting():
    s = r"Built \textbf{FastAPI} service, 40\% faster, 3$\times$ throughput, \emph{R\&D}"
    assert bankmod.safe_latex(s) == s


def test_r_squared_prints_as_r_squared():
    s = r"Trained a TCN to test $R^2$ 0.56 on 3 held-out subjects."
    # not escaped into a literal "$R^2$", and not a math superscript (smaller than the page's minimum font)
    assert bankmod.safe_latex(s) == r"Trained a TCN to test R\texttwosuperior{} 0.56 on 3 held-out subjects."
    assert bankmod.plain(s) == "Trained a TCN to test R² 0.56 on 3 held-out subjects."
    assert "2" not in bankmod.numbers(s)                           # the exponent isn't a claimed number
    assert bankmod.safe_latex(r"$a^2$") == r"\$a\textasciicircum{}2\$"   # other math stays escaped
