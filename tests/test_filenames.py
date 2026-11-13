import pytest

import pipeline

BANK = {"profile": {"first_name": "Krish", "last_name": "Bhagirath"}, "settings": {}}


@pytest.mark.parametrize("company,role,expected", [
    ("Shopify", "Software Engineering Intern (Backend)", "Krish_Bhagirath_Shopify_SoftwareEngineeringIntern_Resume.pdf"),
    ("Jane Street Capital, LLC", "Software Engineer Intern - Summer 2026", "Krish_Bhagirath_JaneStreetCapital_SoftwareEngineerIntern_Resume.pdf"),
    ("Café Inc.", "AI/ML Intern", "Krish_Bhagirath_Cafe_AIMLIntern_Resume.pdf"),
    ("AT&T", "C++ Developer", "Krish_Bhagirath_ATAndT_CppDeveloper_Resume.pdf"),
    ("", "", "Krish_Bhagirath_Company_Resume.pdf"),
    ("../../etc", "<script>", "Krish_Bhagirath_Etc_Script_Resume.pdf"),
])
def test_filenames(company, role, expected):
    assert pipeline.pdf_filename(BANK, company, role) == expected


def test_filename_pattern_setting():
    b = {**BANK, "settings": {"filename_pattern": "{first}_{last}_Resume_{company}"}}
    assert pipeline.pdf_filename(b, "Acme", "Dev") == "Krish_Bhagirath_Resume_Acme.pdf"
