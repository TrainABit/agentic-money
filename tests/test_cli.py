from sovereign.cli import main


def test_cli_init_and_status(tmp_path, capsys):
    code = main(["init", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    out = capsys.readouterr().out
    assert "eth_address" in out
    code = main(["doctor", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    code = main(["wallet", "--data-dir", str(tmp_path)])
    assert code == 0
