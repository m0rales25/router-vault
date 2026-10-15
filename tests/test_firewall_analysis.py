import unittest
from app.firewall_analysis import analyze, parse_export

class AnalysisTests(unittest.TestCase):
    def test_raw_nat_multiline(self):
        cfg = '/ip firewall raw\nadd chain=prerouting action=drop \\\n src-address-list=rv_threat_ips\n/ip firewall nat\nadd chain=dstnat action=dst-nat'
        a = analyze(cfg)
        self.assertEqual(a["threat_drop_rules"], 1)
        self.assertEqual(a["dstnat_rules"], 1)

    def test_disabled(self):
        self.assertEqual(analyze('/ip firewall raw\nadd action=drop src-address-list=rv_threat_ips disabled=yes')["threat_drop_rules"], 0)

    def test_geoblock_warning(self):
        a = analyze('/ip firewall raw\nadd chain=prerouting action=drop src-address-list=!rv_gr_ranges')
        self.assertIn('GEO_DROP_BROAD', [f["code"] for f in a["findings"]])
        self.assertIn('GEO_DROP_UNSCOPED', [f["code"] for f in a["findings"]])

    def test_precedence(self):
        a = analyze('/ip firewall raw\nadd chain=prerouting action=accept src-address-list=rv_whitelist\nadd chain=prerouting action=drop src-address-list=rv_threat_ips')
        self.assertIn('RAW_PRECEDENCE_REVIEW', [f["code"] for f in a["findings"]])

    def test_unsupported_script_ignored(self):
        a = parse_export('/system script\nadd name=x source=":put hi"\n/ip firewall raw\n:execute script=x\nadd action=drop')
        self.assertEqual(len(a), 1)

if __name__ == "__main__": unittest.main()
