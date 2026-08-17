from sovereign.cli import _config, build_parser, main


def test_cli_init_and_status(tmp_path, capsys):
    code = main(["init", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    out = capsys.readouterr().out
    assert "eth_address" in out
    code = main(["doctor", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    code = main(["wallet", "--data-dir", str(tmp_path)])
    assert code == 0


def test_cli_loads_yaml_with_cli_globals_winning(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "daily_apply_cap: 3\n"
        "tick_seconds: 45\n"
        "mode: live\n"
    )
    args = build_parser().parse_args(
        ["status", "--data-dir", str(tmp_path), "--mode", "sim"]
    )
    config = _config(args)
    assert config.daily_apply_cap == 3
    assert config.tick_seconds == 45
    assert config.mode == "sim"


def test_cli_reports_clean_errors_and_requires_live_paid_confirmation(tmp_path, capsys):
    code = main(["accept", "--data-dir", str(tmp_path), "job_missing"])
    assert code == 1
    captured = capsys.readouterr()
    assert "job_missing" in captured.err
    assert "Traceback" not in captured.err

    code = main(
        [
            "paid",
            "--data-dir",
            str(tmp_path),
            "--mode",
            "live",
            "inv_missing",
        ]
    )
    assert code == 1
    assert "--confirm" in capsys.readouterr().out
