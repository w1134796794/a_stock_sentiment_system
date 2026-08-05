from desktop.runner import LogBuffer


def test_log_buffer_strips_ansi_without_damaging_chinese():
    buffer = LogBuffer()

    buffer.append_line("\x1b[32m2026-08-02\x1b[0m | \x1b[1m因子计算完成\x1b[0m")
    buffer.append_text("\x1b[36m首板共振\x1b[0m\n")

    lines, cursor = buffer.read_from(0)
    assert lines == ["2026-08-02 | 因子计算完成", "首板共振"]
    assert cursor == 2
