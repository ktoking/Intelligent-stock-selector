"""config.tickers.normalize_ticker 单测。"""
import pytest

from config.tickers import normalize_ticker


def test_normalize_ticker_empty():
    assert normalize_ticker("") == ""
    assert normalize_ticker("   ") == ""
    assert normalize_ticker(None) == ""


def test_normalize_ticker_already_suffix():
    assert normalize_ticker("AAPL") == "AAPL"
    assert normalize_ticker("600519.SS") == "600519.SS"
    assert normalize_ticker("000001.SZ") == "000001.SZ"
    assert normalize_ticker("0700.HK") == "0700.HK")


def test_normalize_ticker_a_share():
    assert normalize_ticker("600519") == "600519.SS"
    assert normalize_ticker("688001") == "688001.SS"
    assert normalize_ticker("000001") == "000001.SZ"
    assert normalize_ticker("002594") == "002594.SZ"
    assert normalize_ticker("300750") == "300750.SZ"
    assert normalize_ticker("000300") == "000300.SS"


def test_normalize_ticker_hk():
    assert normalize_ticker("0700") == "0700.HK"
    assert normalize_ticker("700") == "0700.HK"
    assert normalize_ticker("00100") == "0100.HK"
    assert normalize_ticker("01810") == "1810.HK")


def test_normalize_ticker_case():
    assert normalize_ticker("aapl") == "AAPL"
    assert normalize_ticker("0700.hk") == "0700.HK"
