part of 'marketing_studio_screen.dart';

// ── What a turn looks like ──────────────────────────────────────────────────
//
// Zeno's answers arrive as blocks (a lead, bullets or steps, a pasteable
// draft, a next step), so a useful answer reads as structure rather than one
// grey paragraph. Actions sit under the answer that needs them, and every
// button here opens a real screen.

class _ZenoUserBubble extends StatelessWidget {
  const _ZenoUserBubble({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerRight,
        child: Container(
          constraints:
              BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.82),
          padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [
                _accent.withOpacity(0.24),
                _accent.withOpacity(0.12),
              ],
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
            ),
            borderRadius: const BorderRadius.only(
              topLeft: Radius.circular(16),
              topRight: Radius.circular(16),
              bottomLeft: Radius.circular(16),
              bottomRight: Radius.circular(5),
            ),
            border: Border.all(color: _accent.withOpacity(0.32)),
          ),
          child: Text(text,
              style: const TextStyle(
                  color: BrokaColors.textHigh, fontSize: 13.5, height: 1.45)),
        ),
      );
}

class _ZenoTyping extends StatelessWidget {
  const _ZenoTyping();

  @override
  Widget build(BuildContext context) => Row(
        key: const Key('zeno-typing'),
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          const _ZenoMark(size: 26),
          const SizedBox(width: 9),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
            decoration: BoxDecoration(
              color: BrokaColors.bgCard.withOpacity(0.6),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: BrokaColors.border),
            ),
            child: const Row(mainAxisSize: MainAxisSize.min, children: [
              SizedBox(
                width: 13,
                height: 13,
                child: CircularProgressIndicator(
                    strokeWidth: 1.8, color: BrokaColors.gold),
              ),
              SizedBox(width: 9),
              Text('Reading your store...',
                  style: TextStyle(
                      color: BrokaColors.textMid,
                      fontSize: 12,
                      fontStyle: FontStyle.italic)),
            ]),
          ),
        ],
      );
}

class _ZenoError extends StatelessWidget {
  const _ZenoError({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Container(
        key: const Key('zeno-error'),
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
        decoration: BoxDecoration(
          color: BrokaColors.bgCard.withOpacity(0.6),
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: BrokaColors.border),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Row(children: [
            Icon(Icons.cloud_off_rounded, size: 16, color: BrokaColors.textMid),
            SizedBox(width: 8),
            Text("Zeno couldn't answer",
                style: TextStyle(
                    color: BrokaColors.textHigh,
                    fontSize: 13,
                    fontWeight: FontWeight.w700)),
          ]),
          const SizedBox(height: 6),
          Text(message,
              style: const TextStyle(
                  color: BrokaColors.textMid, fontSize: 12.5, height: 1.45)),
          const SizedBox(height: 8),
          Row(children: [
            FilledButton.tonalIcon(
              key: const Key('zeno-retry'),
              onPressed: onRetry,
              icon: const Icon(Icons.refresh_rounded, size: 16),
              label: const Text('Try again'),
              style: FilledButton.styleFrom(
                backgroundColor: BrokaColors.bgMid,
                foregroundColor: BrokaColors.textHigh,
                minimumSize: const Size(0, 38),
              ),
            ),
            const SizedBox(width: 8),
            const Expanded(
              child: Text('Your question is kept.',
                  style: TextStyle(color: BrokaColors.textLow, fontSize: 10.5)),
            ),
          ]),
        ]),
      );
}

class _ZenoAnswer extends StatelessWidget {
  const _ZenoAnswer({
    required this.reply,
    required this.index,
    required this.onAction,
  });
  final SidekickReply reply;
  final int index;
  final ValueChanged<SidekickAction> onAction;

  @override
  Widget build(BuildContext context) {
    final sections = reply.sections.isNotEmpty
        ? reply.sections
        : [ZenoSection(kind: 'text', text: reply.reply)];
    return Container(
      key: Key('zeno-answer-$index'),
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(13, 12, 13, 13),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            BrokaColors.bgCard.withOpacity(0.85),
            _accent.withOpacity(0.05),
          ],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        borderRadius: const BorderRadius.only(
          topLeft: Radius.circular(5),
          topRight: Radius.circular(18),
          bottomLeft: Radius.circular(18),
          bottomRight: Radius.circular(18),
        ),
        border: Border.all(color: _accent.withOpacity(0.16)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            const _ZenoMark(size: 24),
            const SizedBox(width: 8),
            const Text('Zeno',
                style: TextStyle(
                    color: BrokaColors.textHigh,
                    fontSize: 12,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.1)),
            const Spacer(),
            _ZenoSourceChip(available: reply.available),
          ]),
          const SizedBox(height: 10),
          for (var i = 0; i < sections.length; i++)
            Padding(
              padding:
                  EdgeInsets.only(bottom: i == sections.length - 1 ? 0 : 9),
              child: _ZenoBlock(
                section: sections[i],
                keyPrefix: '$index-$i',
              ),
            ),
          if (reply.fallbackReason != null) ...[
            const SizedBox(height: 9),
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Padding(
                padding: EdgeInsets.only(top: 1),
                child: Icon(Icons.info_outline_rounded,
                    size: 13, color: BrokaColors.textLow),
              ),
              const SizedBox(width: 6),
              Expanded(
                child: Text(reply.fallbackReason!,
                    style: const TextStyle(
                        color: BrokaColors.textLow,
                        fontSize: 10.5,
                        height: 1.4)),
              ),
            ]),
          ],
          if (reply.products.isNotEmpty) ...[
            const SizedBox(height: 11),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final product in reply.products)
                  _ZenoProductPill(product: product),
              ],
            ),
          ],
          if (reply.actions.isNotEmpty) ...[
            const SizedBox(height: 11),
            Wrap(
              spacing: 7,
              runSpacing: 7,
              children: [
                for (final action in reply.actions)
                  _ZenoActionButton(
                      action: action, onTap: () => onAction(action)),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _ZenoSourceChip extends StatelessWidget {
  const _ZenoSourceChip({required this.available});
  final bool available;

  @override
  Widget build(BuildContext context) {
    final colour = available ? _accent : BrokaColors.gold;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
      decoration: BoxDecoration(
        color: colour.withOpacity(0.12),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        available ? 'Your store + AI' : 'BROKA guidance',
        style: TextStyle(
            color: colour,
            fontSize: 9.5,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.15),
      ),
    );
  }
}

class _ZenoBlock extends StatelessWidget {
  const _ZenoBlock({required this.section, required this.keyPrefix});
  final ZenoSection section;
  final String keyPrefix;

  @override
  Widget build(BuildContext context) {
    switch (section.kind) {
      case 'bullets':
        return Column(
          key: Key('zeno-bullets-$keyPrefix'),
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (final item in section.items) _ZenoBullet(text: item),
          ],
        );
      case 'steps':
        return Column(
          key: Key('zeno-steps-$keyPrefix'),
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (var i = 0; i < section.items.length; i++)
              _ZenoStep(number: i + 1, text: section.items[i]),
          ],
        );
      case 'copy':
        return Container(
          key: Key('zeno-copy-block-$keyPrefix'),
          width: double.infinity,
          padding: const EdgeInsets.fromLTRB(11, 8, 8, 10),
          decoration: BoxDecoration(
            color: BrokaColors.bgMid.withOpacity(0.55),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: BrokaColors.border),
          ),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Expanded(
                child: Text(
                  (section.title.isEmpty ? 'Ready to paste' : section.title)
                      .toUpperCase(),
                  style: const TextStyle(
                      color: BrokaColors.textLow,
                      fontSize: 9,
                      letterSpacing: 0.9,
                      fontWeight: FontWeight.w700),
                ),
              ),
              TextButton.icon(
                key: Key('zeno-copy-$keyPrefix'),
                onPressed: () => _copy(context, section.text, 'Draft'),
                icon: const Icon(Icons.copy_all_rounded, size: 14),
                label: const Text('Copy', style: TextStyle(fontSize: 11)),
                style: TextButton.styleFrom(
                  foregroundColor: BrokaColors.gold,
                  minimumSize: Size.zero,
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              ),
            ]),
            const SizedBox(height: 2),
            SelectableText(
              section.text,
              style: const TextStyle(
                  color: BrokaColors.textHigh,
                  fontSize: 13,
                  height: 1.5,
                  letterSpacing: 0.05),
            ),
          ]),
        );
      case 'next':
        return Container(
          key: Key('zeno-next-$keyPrefix'),
          width: double.infinity,
          padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
          decoration: BoxDecoration(
            color: BrokaColors.gold.withOpacity(0.08),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: BrokaColors.gold.withOpacity(0.22)),
          ),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Padding(
              padding: EdgeInsets.only(top: 1),
              child: Icon(Icons.arrow_outward_rounded,
                  size: 14, color: BrokaColors.gold),
            ),
            const SizedBox(width: 7),
            Expanded(
              child: Text(section.text,
                  style: const TextStyle(
                      color: BrokaColors.textHigh,
                      fontSize: 12,
                      height: 1.4,
                      fontWeight: FontWeight.w500)),
            ),
          ]),
        );
      default:
        return SelectableText(
          section.text,
          key: Key('zeno-text-$keyPrefix'),
          style: const TextStyle(
              color: BrokaColors.textHigh, fontSize: 13.5, height: 1.55),
        );
    }
  }
}

class _ZenoBullet extends StatelessWidget {
  const _ZenoBullet({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 5),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Padding(
            padding: EdgeInsets.only(top: 6),
            child: SizedBox(
              width: 5,
              height: 5,
              child: DecoratedBox(
                decoration: BoxDecoration(
                    color: BrokaColors.gold, shape: BoxShape.circle),
              ),
            ),
          ),
          const SizedBox(width: 9),
          Expanded(
            child: Text(text,
                style: const TextStyle(
                    color: BrokaColors.textHigh, fontSize: 13, height: 1.5)),
          ),
        ]),
      );
}

class _ZenoStep extends StatelessWidget {
  const _ZenoStep({required this.number, required this.text});
  final int number;
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 7),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Container(
            width: 20,
            height: 20,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: _accent.withOpacity(0.14),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text('$number',
                style: const TextStyle(
                    color: _accent,
                    fontSize: 10.5,
                    fontWeight: FontWeight.w800)),
          ),
          const SizedBox(width: 9),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.only(top: 1),
              child: Text(text,
                  style: const TextStyle(
                      color: BrokaColors.textHigh, fontSize: 13, height: 1.45)),
            ),
          ),
        ]),
      );
}

class _ZenoProductPill extends StatelessWidget {
  const _ZenoProductPill({required this.product});
  final SidekickProduct product;

  @override
  Widget build(BuildContext context) {
    final bits = [
      product.name,
      product.priceLabel,
      if (product.availableUnits != null)
        '${product.availableUnits} left'
      else
        null,
    ].whereType<String>().where((s) => s.isNotEmpty);
    return Container(
      key: Key('zeno-product-${product.id}'),
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      decoration: BoxDecoration(
        color: BrokaColors.bgMid.withOpacity(0.5),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: BrokaColors.border),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.inventory_2_outlined,
            size: 12, color: BrokaColors.textMid),
        const SizedBox(width: 5),
        Flexible(
          child: Text(bits.join(' · '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                  color: BrokaColors.textMid,
                  fontSize: 11,
                  fontWeight: FontWeight.w500)),
        ),
      ]),
    );
  }
}

class _ZenoActionButton extends StatelessWidget {
  const _ZenoActionButton({required this.action, required this.onTap});
  final SidekickAction action;
  final VoidCallback onTap;

  static const _maxWidth = 230.0;

  IconData get _icon => switch (action.type) {
        'creative_studio' => Icons.palette_outlined,
        'platform_guide' => Icons.menu_book_outlined,
        'video_script' => Icons.videocam_outlined,
        'meta_ads' => Icons.campaign_outlined,
        _ => Icons.open_in_new_rounded,
      };

  @override
  Widget build(BuildContext context) {
    final creative = action.type == 'creative_studio';
    final colour = creative ? BrokaColors.gold : BrokaColors.textHigh;
    // A long label ("Plan a Meta ad for Black Leather Handbag") must wrap
    // instead of pushing the card sideways on a 320 px phone.
    return ConstrainedBox(
      constraints: BoxConstraints(
          maxWidth: _maxWidth.clamp(0, MediaQuery.sizeOf(context).width - 56)),
      child: Material(
        color: BrokaColors.bgMid.withOpacity(0.45),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(11),
          side: BorderSide(
              color: creative
                  ? BrokaColors.gold.withOpacity(0.55)
                  : BrokaColors.border),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          key: Key('zeno-action-${action.type}'
              '${action.platform == null ? '' : '-${action.platform}'}'),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Icon(_icon, size: 14, color: colour),
              const SizedBox(width: 6),
              Flexible(
                child: Text(action.label,
                    style: TextStyle(
                        color: colour,
                        fontSize: 11.5,
                        height: 1.3,
                        fontWeight: FontWeight.w600)),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}
