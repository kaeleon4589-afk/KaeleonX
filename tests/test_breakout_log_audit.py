from tools.audit_breakout_logs import report, parse_events


def test_breakout_report_deduplicates_and_does_not_claim_net_pnl(tmp_path):
    log = tmp_path / "log.txt"
    def line(e):
        import json
        return '2026 [inf] KAELEON event='+e['event']+' payload='+json.dumps(e)
    log.write_text('\n'.join(map(line, [
        {"event":"SETUP_ARMED_PROGRESS","strategy":"BREAKOUT_RETEST","reason":"micro_confirmation_pending"},
        {"event":"SETUP_ARMED_PROGRESS","strategy":"BREAKOUT_RETEST","reason":"micro_confirmation_pending"},
        {"event":"SETUP_TRIGGERED","strategy":"BREAKOUT_RETEST","user_id":"u1","setup_id":"s1","trace":{"confirmation_mode":"postarm_closed_1m"}},
        {"event":"SETUP_TRIGGERED","strategy":"BREAKOUT_RETEST","user_id":"u1","setup_id":"s1","trace":{"confirmation_mode":"postarm_closed_1m"}},
        {"event":"POSITION_OPENED","strategy":"BREAKOUT_RETEST","position_id":"p1"},
        {"event":"POSITION_CLOSED","strategy":"BREAKOUT_RETEST","position_id":"p1","gross_pnl":-2.5},
        {"event":"POSITION_CLOSED","strategy":"LIQUIDITY_SWEEP","position_id":"p2","gross_pnl":2.0},
    ])), encoding='utf8')
    info = report(parse_events([str(log)]))
    assert info['distinct_triggered_user_setups'] == 1
    assert info['confirmation_modes'] == {'postarm_closed_1m':1}
    assert info['gross_pnl_usdt_before_fees'] == -2.5
    assert info['pending_poll_counts_not_unique_signals']['micro_confirmation_pending'] == 2
    assert 'neto' in info['note']
