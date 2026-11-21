part of 'marketing_studio_screen.dart';

// ── The tools, kept one tap away ────────────────────────────────────────────
//
// Three compact chips under Zeno's name, not a second navigation bar and not
// three more places to get help. Each opens a real screen on top of the
// conversation; coming back leaves the thread exactly where it was.

class _StudioToolsRow extends StatelessWidget {
  const _StudioToolsRow({
    required this.onCreative,
    required this.onCampaigns,
    required this.onAds,
  });
  final VoidCallback onCreative;
  final VoidCallback onCampaigns;
  final VoidCallback onAds;

  @override
  Widget build(BuildContext context) => SizedBox(
        height: 42,
        child: ListView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.fromLTRB(16, 2, 16, 8),
          children: [
            _StudioToolChip(
              key: const Key('marketing-tool-creative'),
              icon: Icons.palette_outlined,
              label: 'Creative',
              onTap: onCreative,
            ),
            const SizedBox(width: 7),
            _StudioToolChip(
              key: const Key('marketing-tool-campaigns'),
              icon: Icons.collections_bookmark_outlined,
              label: 'Campaigns',
              onTap: onCampaigns,
            ),
            const SizedBox(width: 7),
            _StudioToolChip(
              key: const Key('marketing-tool-ads'),
              icon: Icons.campaign_outlined,
              label: 'Meta Ads',
              onTap: onAds,
            ),
          ],
        ),
      );
}

class _StudioToolChip extends StatelessWidget {
  const _StudioToolChip(
      {super.key,
      required this.icon,
      required this.label,
      required this.onTap});
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: BrokaColors.bgCard.withOpacity(0.55),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(20),
          side: BorderSide(color: BrokaColors.border.withOpacity(0.9)),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Icon(icon, size: 14, color: BrokaColors.textMid),
              const SizedBox(width: 6),
              Text(label,
                  style: const TextStyle(
                      color: BrokaColors.textMid,
                      fontSize: 11.5,
                      fontWeight: FontWeight.w600)),
            ]),
          ),
        ),
      );
}

/// A tool's own screen: a title, one line saying what it is for, then the tool
/// itself (the same widget the Studio always had). The conversation is
/// underneath and untouched.
class _StudioToolScreen extends StatelessWidget {
  const _StudioToolScreen({
    required this.title,
    required this.subtitle,
    required this.child,
  });
  final String title;
  final String subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: BrokaColors.bg,
        appBar: AppBar(
          backgroundColor: BrokaColors.bg,
          surfaceTintColor: Colors.transparent,
          foregroundColor: BrokaColors.textHigh,
          elevation: 0,
          toolbarHeight: 62,
          titleSpacing: 0,
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Text(title,
                  style: const TextStyle(
                      color: BrokaColors.textHigh,
                      fontSize: 16.5,
                      fontWeight: FontWeight.w800,
                      letterSpacing: -0.2)),
              const SizedBox(height: 1),
              Text(subtitle,
                  style: const TextStyle(
                      color: BrokaColors.textMid, fontSize: 11)),
            ],
          ),
        ),
        body: child,
      );
}
