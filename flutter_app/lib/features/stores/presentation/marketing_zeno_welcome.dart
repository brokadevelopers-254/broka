part of 'marketing_studio_screen.dart';

// ── Before the first question ───────────────────────────────────────────────
//
// Four sentences, four ways in. No guide, no checklist, no page of marketing
// theory: the seller should be able to ask something in five seconds.

class _ZenoWelcome extends StatelessWidget {
  const _ZenoWelcome({required this.store, required this.onAsk});
  final Store store;
  final ValueChanged<String> onAsk;

  static const _starters = <({String text, IconData icon})>[
    (
      text: 'Which of my products should I promote first?',
      icon: Icons.inventory_2_outlined
    ),
    (
      text: 'Write a caption for one of my products',
      icon: Icons.edit_note_rounded
    ),
    (
      text: 'Help me plan a Meta ad with a budget I choose',
      icon: Icons.campaign_outlined
    ),
    (
      text: 'How do I get more people to discover my store?',
      icon: Icons.travel_explore_rounded
    ),
  ];

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const Key('zeno-welcome'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 8),
        Row(children: [
          const _ZenoMark(size: 54),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text("Hi, I'm Zeno.",
                    style: TextStyle(
                        color: BrokaColors.textHigh,
                        fontSize: 23,
                        height: 1.15,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -0.5)),
                const SizedBox(height: 3),
                Text(
                  'Your marketing partner at ${store.name}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      color: BrokaColors.textMid, fontSize: 12.5),
                ),
              ],
            ),
          ),
        ]),
        const SizedBox(height: 16),
        const Text(
          "I can see what you sell and what you're already trying. Ask me "
          "anything about selling more - we'll work it out together.",
          style: TextStyle(
              color: BrokaColors.textMid, fontSize: 13.5, height: 1.55),
        ),
        const SizedBox(height: 20),
        Padding(
          padding: const EdgeInsets.only(left: 2),
          child: Text('START A CONVERSATION',
              style: TextStyle(
                  color: BrokaColors.textLow.withOpacity(0.95),
                  fontSize: 9.5,
                  letterSpacing: 1.1,
                  fontWeight: FontWeight.w700)),
        ),
        const SizedBox(height: 8),
        for (var i = 0; i < _starters.length; i++)
          Padding(
            padding: const EdgeInsets.only(bottom: 7),
            child: _StarterTile(
              key: Key('zeno-starter-$i'),
              icon: _starters[i].icon,
              text: _starters[i].text,
              onTap: () => onAsk(_starters[i].text),
            ),
          ),
        const SizedBox(height: 14),
        Container(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
          decoration: BoxDecoration(
            color: BrokaColors.bgCard.withOpacity(0.4),
            borderRadius: BorderRadius.circular(13),
            border: Border.all(color: BrokaColors.border.withOpacity(0.7)),
          ),
          child: const Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: EdgeInsets.only(top: 1),
                child: Icon(Icons.verified_user_outlined,
                    size: 15, color: BrokaColors.textLow),
              ),
              SizedBox(width: 8),
              Expanded(
                child: Text(
                  "I use your real products, prices and counted campaign "
                  "activity. I never invent a price or a result - if I don't "
                  "know something, I'll ask.",
                  style: TextStyle(
                      color: BrokaColors.textLow, fontSize: 11, height: 1.45),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 8),
        const Padding(
          padding: EdgeInsets.only(left: 2),
          child: Text(
            'This conversation stays on this screen until you leave the Studio.',
            style: TextStyle(
                color: BrokaColors.textLow, fontSize: 10, height: 1.4),
          ),
        ),
        const SizedBox(height: 4),
      ],
    );
  }
}

class _StarterTile extends StatelessWidget {
  const _StarterTile(
      {super.key, required this.icon, required this.text, required this.onTap});
  final IconData icon;
  final String text;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: BrokaColors.bgCard.withOpacity(0.55),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
          side: BorderSide(color: BrokaColors.border.withOpacity(0.85)),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(11, 11, 10, 11),
            child: Row(children: [
              Container(
                width: 32,
                height: 32,
                decoration: BoxDecoration(
                  color: _accent.withOpacity(0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Icon(icon, size: 17, color: _accent),
              ),
              const SizedBox(width: 11),
              Expanded(
                child: Text(text,
                    style: const TextStyle(
                        color: BrokaColors.textHigh,
                        fontSize: 12.5,
                        height: 1.35,
                        fontWeight: FontWeight.w500)),
              ),
              const SizedBox(width: 6),
              const Icon(Icons.arrow_forward_rounded,
                  size: 15, color: BrokaColors.textLow),
            ]),
          ),
        ),
      );
}
