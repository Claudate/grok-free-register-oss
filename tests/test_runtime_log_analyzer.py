import unittest

from tools.runtime_log_analyzer import (
    parse_monitor_lines,
    parse_solver_timelines,
    summarize_monitor_rows,
    summarize_solver_timelines,
)


class RuntimeLogAnalyzerTests(unittest.TestCase):
    def test_parses_csp_monitor_rows_and_recent_rates(self):
        text = "\n".join(
            [
                "[*] T:0 Q:3 phys:0 t_slot:3 q_slot:0 q_pend:0 "
                "t_prod:76 t_adm:76 t_exp:0 q_sent:87 q_ret:87 q_adm:79 q_exp:0 "
                "pair:76 ok:71 fail:0 rate:8.6/min #71",
                "[*] T:0 Q:3 phys:0 t_slot:3 q_slot:0 q_pend:0 "
                "t_prod:91 t_adm:91 t_exp:0 q_sent:101 q_ret:101 q_adm:94 q_exp:0 "
                "pair:91 ok:86 fail:0 rate:8.8/min #86",
            ]
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(rows[0].kind, "csp")
        self.assertEqual(summary["last_ok"], 86)
        self.assertAlmostEqual(summary["last_cumulative_rate"], 8.8)
        self.assertAlmostEqual(summary["recent_ok_per_min"], 9.89, places=2)
        self.assertAlmostEqual(summary["recent_t_prod_per_min"], 9.89, places=2)
        self.assertEqual(summary["last_q_minus_t"], 3)
        self.assertEqual(summary["last_q_return_minus_t_prod"], 10)

    def test_parses_state_machine_monitor_rows(self):
        text = "\n".join(
            [
                "[*] slots:7/8 act:7 cpu:83% avg:76%/85 mem:4841M "
                "T:4 Q:0 sent:0 got:0(0%) rate:0.0/min #0",
                "[*] slots:8/8 act:8 cpu:91% avg:88%/85 mem:5012M "
                "T:6 Q:5 sent:61 got:48(79%) rate:7.8/min #40",
                "[*] slots:6/8 act:6 cpu:100% avg:90%/85 mem:5351M "
                "T:1 Q:6 sent:68 got:68(100%) rate:8.8/min #59",
            ]
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(rows[-1].kind, "state_machine")
        self.assertEqual(summary["last_ok"], 59)
        self.assertAlmostEqual(summary["last_cumulative_rate"], 8.8)
        self.assertEqual(summary["last_q_minus_t"], 5)
        self.assertEqual(summary["last_slots"], 6)

    def test_parses_csp_v2_monitor_rows_with_admission_fields(self):
        text = (
            "[*] T:1 Q:2 phys:3 p_send:1 t_slot:4 q_slot:5 q_pend:6 "
            "p_batch:3.5 t_prog:1 q_inflight:4 "
            "s_phys:0.50/3.00 p_phys:0.20/1.40 c_phys:0.30/2.50 "
            "p_stage:0.50/0.80/0.40 c_stage:0.30/0.40/1.50 c_hot:4/1 "
            "solver_goto:0.10 solver_inject:0.20 solver_initial:0.50 "
            "solver_click:0.01 solver_wait:11.80 solver_reuse:0.75 solver_visible:0.00 "
            "t_prod:20 t_adm:18 t_exp:1 q_sent:24 q_ret:22 q_adm:20 q_exp:0 "
            "pair:17 ok:16 fail:1 rate:9.1/min #16"
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "csp")
        self.assertEqual(summary["last_ok"], 16)
        self.assertEqual(summary["last_q_return_minus_t_prod"], 2)
        self.assertEqual(summary["last_solver_wait"], 11.8)
        self.assertEqual(summary["last_solver_reuse"], 0.75)
        self.assertEqual(summary["last_phys"], 3)
        self.assertEqual(summary["last_p_batch"], 3.5)
        self.assertEqual(summary["last_t_prog"], 1)
        self.assertEqual(summary["last_q_inflight"], 4)
        self.assertEqual(summary["last_s_phys_hold"], 3.0)
        self.assertEqual(summary["last_p_phys_wait"], 0.2)
        self.assertEqual(summary["last_c_phys_hold"], 2.5)
        self.assertEqual(summary["last_physical_hold_leader"], "s")
        self.assertEqual(summary["last_physical_wait_leader"], "s")
        self.assertEqual(summary["last_p_email_create"], 0.5)
        self.assertEqual(summary["last_p_page_prepare"], 0.8)
        self.assertEqual(summary["last_p_send"], 0.4)
        self.assertEqual(summary["last_c_page_acquire"], 0.3)
        self.assertEqual(summary["last_c_verify"], 0.4)
        self.assertEqual(summary["last_c_register"], 1.5)
        self.assertEqual(summary["last_c_hot_hits"], 4)
        self.assertEqual(summary["last_c_hot_misses"], 1)

    def test_summarizes_solver_timeline_events(self):
        text = (
            '[solver_timeline] [{"t":0.0,"event":"page_trace_after_inject",'
            '"page_trace":{"created_at":0.0,"render_called_at":100.0,"render_returned_at":120.0,'
            '"token_written_at":null}},'
            '{"t":0.5,"event":"click_before","attempt":1,"token_len":0,'
            '"dom":{"widget":{"present":true,"visible":true},'
            '"turnstile_iframe_count":0,'
            '"element_at_center":{"tag":"DIV","is_iframe":false}}},'
            '{"t":3.0,"event":"click_after","attempt":1,"token_len":0,'
            '"click_call_ms":2500.0,'
            '"click_trace":{"mouse_move1_ms":500.0,"mouse_move2_ms":1500.0,'
            '"mouse_down_ms":300.0,"mouse_up_ms":100.0}},'
            '{"t":3.0,"event":"poll_start"},'
            '{"t":4.0,"event":"poll_done","ok":true,"token_len":730,'
            '"poll_attempts":2,"first_token_attempt":2,"poll_read_ms_avg":12.0,'
            '"poll_read_ms_max":20.0,'
            '"page_trace":{"created_at":0.0,"render_called_at":100.0,"render_returned_at":120.0,'
            '"token_written_at":3900.0}}]'
        )

        timelines = parse_solver_timelines(text)
        summary = summarize_solver_timelines(timelines)

        self.assertEqual(len(timelines), 1)
        self.assertEqual(summary["solver_timeline_count"], 1)
        self.assertEqual(summary["ok_count"], 1)
        self.assertEqual(summary["avg_click_call_ms"], 2500.0)
        self.assertEqual(summary["avg_click_mouse_move_ms"], 2000.0)
        self.assertEqual(summary["avg_render_to_token_ms"], 3800.0)
        self.assertEqual(summary["avg_token_write_to_poll_done_ms"], 100.0)
        self.assertEqual(summary["avg_poll_attempts"], 2.0)
        self.assertEqual(summary["center_iframe_hit_ratio"], 0.0)
        self.assertEqual(summary["turnstile_iframe_seen_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
