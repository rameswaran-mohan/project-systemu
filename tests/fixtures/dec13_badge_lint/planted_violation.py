"""PLANTED DEC-13 violations -- the badge lint's self-test (never imported).

A surface that decides the badge itself: it reads the mint's private inputs off
the receipt, spells the verdicts, uses the filled check directly and reads the
receipt store behind the mint's back. Every line below must be reported.
"""
from systemu.runtime.receipts_store import read_receipts


def render(receipt, ui):
    if receipt.get("confirmed") is True and receipt["shadow"] is not True:
        ui.label("Verified").props("icon=check_circle")
    else:
        ui.label("claimed")
    return read_receipts("exec_1")


def badge_icon(evidence):
    return "check_circle" if evidence.confirmed else "radio_button_unchecked"
