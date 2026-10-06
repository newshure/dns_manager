"""SOA serial 정책 테스트."""

from __future__ import annotations

from datetime import date

import pytest

from dns_manager.core import serial

TODAY = date(2026, 10, 1)
BASE = 2026100100


def test_first_serial_of_the_day():
    assert serial.next_serial(None, TODAY) == BASE + 1


def test_older_serial_jumps_to_today():
    assert serial.next_serial(2026093005, TODAY) == BASE + 1


def test_same_day_increments():
    assert serial.next_serial(BASE + 1, TODAY) == BASE + 2
    assert serial.next_serial(BASE + 98, TODAY) == BASE + 99


def test_saturated_day_still_increases():
    """하루 100회를 넘겨도 serial 은 반드시 증가해야 한다(감소하면 secondary 가 멈춘다)."""
    assert serial.next_serial(BASE + 99, TODAY) == BASE + 100


def test_future_serial_is_not_rolled_back():
    future = 2030010101
    assert serial.next_serial(future, TODAY) == future + 1


def test_overflow_raises():
    with pytest.raises(serial.SerialError):
        serial.next_serial(serial.MAX_SERIAL, TODAY)


ZONE_TEXT = """; 주석 유지 확인
$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101  ; serial
                3600        ; refresh
                600         ; retry
                604800      ; expire
                3600 )      ; minimum
@           IN  NS      ns1.example.local.
host2026100101 IN A     10.0.0.1
"""


def test_replace_only_touches_soa_serial():
    out = serial.replace_in_text(ZONE_TEXT, 2026100101, 2026100102)
    assert "2026100102  ; serial" in out
    # 레코드 이름에 들어간 같은 숫자 문자열은 건드리지 않는다
    assert "host2026100101 IN A" in out
    # 주석과 서식은 그대로
    assert out.startswith("; 주석 유지 확인")
    assert "; refresh" in out


def test_replace_is_single_occurrence():
    text = ZONE_TEXT.replace("3600        ; refresh", "2026100101  ; refresh")
    out = serial.replace_in_text(text, 2026100101, 2026100199)
    assert out.count("2026100199") == 1
    assert "2026100101  ; refresh" in out


def test_bump_text():
    out, new = serial.bump_text(ZONE_TEXT, 2026100101, TODAY)
    assert new == 2026100102
    assert str(new) in out


def test_missing_soa_raises():
    with pytest.raises(serial.SerialError):
        serial.replace_in_text("$TTL 3600\n@ IN NS ns1.\n", 1, 2)


def test_missing_serial_value_raises():
    with pytest.raises(serial.SerialError):
        serial.replace_in_text(ZONE_TEXT, 999, 1000)


def test_soa_without_name_or_class():
    text = "$TTL 60\nSOA ns1. admin. ( 7 1 1 1 1 )\n"
    assert "8" in serial.replace_in_text(text, 7, 8)
