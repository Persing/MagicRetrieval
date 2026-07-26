"""merge_partials: concurrent T2 arms write to distinct partial files (avoiding the same-file
race two processes hit when both call report.write("t2_leakage", ...) at once); this combines
them. A duplicate (arm, corpus) across two partials must raise, not silently pick one.
"""

import json

import pytest

from mr import t2_leakage as T2


def _partial(findings_dir, name, results):
    payload = {"results": results, "run_args": {}}
    (findings_dir / f"{name}.json").write_text(json.dumps(payload))


def _fake_result(arm, corpus_name, lift=0.1):
    return {
        "arm": arm, "corpus": corpus_name, "n_decks": 10, "n_cards_vocab": 100,
        "n_positives_mined": 5, "n_positives_strict": 5, "n_positives_trained": 5,
        "n_negatives": 0, "n_holdout_positives": 1, "n_extra_positives_2d": 0,
        "n_extra_negatives_2d": 0, "verdict": "PASS",
        "curated": {
            "n_pairs_in_vocab": 5, "mean_substitute_cosine": 0.8, "mean_random_cluster_cosine": 0.7,
            "mean_random_stratum_cosine": 0.7, "lift_vs_cluster": lift,
            "lift_vs_cluster_ci": [lift - 0.02, lift + 0.02],
            "lift_vs_stratum": lift, "lift_vs_stratum_ci": [lift - 0.02, lift + 0.02],
        },
        "mined_holdout": None,
    }


def test_merge_combines_distinct_arm_corpus_pairs(tmp_path):
    _partial(tmp_path, "t2_leakage_partial_2a", [_fake_result("2a", "precon")])
    _partial(tmp_path, "t2_leakage_partial_casual2b", [_fake_result("2b", "casual")])
    _partial(tmp_path, "t2_leakage_partial_cedh2b", [_fake_result("2b", "cedh")])

    merged = T2.merge_partials(findings_dir=tmp_path)
    keys = {(r["arm"], r["corpus"]) for r in merged["results"]}
    assert keys == {("2a", "precon"), ("2b", "casual"), ("2b", "cedh")}


def test_merge_raises_on_duplicate_arm_corpus(tmp_path):
    _partial(tmp_path, "t2_leakage_partial_a", [_fake_result("2b", "casual", lift=0.1)])
    _partial(tmp_path, "t2_leakage_partial_b", [_fake_result("2b", "casual", lift=0.2)])

    with pytest.raises(ValueError, match="Duplicate"):
        T2.merge_partials(findings_dir=tmp_path)


def test_merge_builds_harness_from_arm_2a_if_present(tmp_path):
    _partial(tmp_path, "t2_leakage_partial_x", [_fake_result("2a", "precon")])
    merged = T2.merge_partials(findings_dir=tmp_path)
    assert merged["harness"]["observed_substitute"] == 0.8


def test_merge_with_no_arm_2a_reports_not_run(tmp_path):
    _partial(tmp_path, "t2_leakage_partial_x", [_fake_result("2b", "casual")])
    merged = T2.merge_partials(findings_dir=tmp_path)
    assert merged["harness"]["corpus_caveat"] == "arm 2a was not run"


def test_merge_writes_canonical_output_files(tmp_path):
    _partial(tmp_path, "t2_leakage_partial_x", [_fake_result("2a", "precon")])
    T2.merge_partials(findings_dir=tmp_path)
    assert (tmp_path / "t2_leakage.json").exists()
    assert (tmp_path / "t2_leakage.md").exists()


def test_merge_ignores_non_matching_files(tmp_path):
    (tmp_path / "unrelated.json").write_text("{}")
    _partial(tmp_path, "t2_leakage_partial_x", [_fake_result("2a", "precon")])
    merged = T2.merge_partials(findings_dir=tmp_path)
    assert len(merged["results"]) == 1
