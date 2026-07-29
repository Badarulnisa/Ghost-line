import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from variant_engine import SeedIdentity, generate_variants


def test_basic_two_name_generation():
    seed = SeedIdentity(names=["ahmed", "chaudhary"])
    variants = generate_variants(seed)
    assert "ahmedchaudhary" in variants
    assert "chaudharyahmed" in variants  # reversed ordering
    assert "ahmed.chaudhary" in variants
    assert "ahmed" in variants  # mononym style


def test_known_handle_mutation():
    seed = SeedIdentity(names=["ahmed"], known_handle="ahmed07")
    variants = generate_variants(seed)
    assert "ahmed07" in variants
    assert "ahmed" in variants  # stripped trailing digits


def test_birth_year_suffixes():
    seed = SeedIdentity(names=["ahmed"], birth_year="1999")
    variants = generate_variants(seed)
    assert "ahmed1999" in variants
    assert "ahmed99" in variants


def test_location_variants():
    seed = SeedIdentity(names=["ahmed"], location="lahore")
    variants = generate_variants(seed)
    assert any("lahore" in v for v in variants)


def test_max_variants_cap():
    seed = SeedIdentity(names=["ahmed", "chaudhary"], location="lahore",
                         profession="developer", birth_year="1999")
    variants = generate_variants(seed, max_variants=15)
    assert len(variants) <= 15


def test_no_empty_or_duplicate_entries():
    seed = SeedIdentity(names=["ahmed", "chaudhary"], location="lahore")
    variants = generate_variants(seed)
    assert "" not in variants
    assert len(variants) == len(set(variants))


def test_requires_at_least_one_name():
    seed = SeedIdentity(names=["", "  "])
    try:
        generate_variants(seed)
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python3", "-m", "pytest", __file__, "-v"])
