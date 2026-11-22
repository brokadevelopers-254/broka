part of 'marketing_studio_screen.dart';

// ── Campaigns ───────────────────────────────────────────────────────────────
//
// What the seller made before. The counts are BROKA's own observations (link
// visits, shares, downloads) - never impressions, spend or sales. Opening a
// campaign shows the previews, edits and tracked links.

class _CampaignsScreen extends StatefulWidget {
  const _CampaignsScreen({required this.store, required this.repo});
  final Store store;
  final StoresRepository repo;

  @override
  State<_CampaignsScreen> createState() => _CampaignsScreenState();
}

class _CampaignsScreenState extends State<_CampaignsScreen> {
  List<MarketingCampaign> _campaigns = const [];
  int _total = 0;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (!mounted) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    final result = await widget.repo.getCampaigns(widget.store.id);
    if (!mounted) return;
    setState(() {
      _loading = false;
      switch (result) {
        case Success(:final data):
          _campaigns = data.$1;
          _total = data.$2;
        case Failure(:final message):
          _error = message;
      }
    });
  }

  Future<void> _showOlder() async {
    final result = await widget.repo
        .getCampaigns(widget.store.id, offset: _campaigns.length);
    if (!mounted) return;
    switch (result) {
      case Success(:final data):
        setState(() {
          _campaigns = [..._campaigns, ...data.$1];
          _total = data.$2;
        });
      case Failure(:final message):
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(message)));
    }
  }

  Future<void> _newCampaign() async {
    await Navigator.of(context).push(MaterialPageRoute(
        builder: (_) =>
            CampaignBuilderScreen(store: widget.store, repo: widget.repo)));
    if (mounted) await _load();
  }

  Future<void> _open(String campaignId) async {
    await Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => CampaignDetailScreen(
            store: widget.store, repo: widget.repo, campaignId: campaignId)));
    if (mounted) await _load();
  }

  @override
  Widget build(BuildContext context) {
    final visits = _campaigns.fold<int>(0, (sum, c) => sum + c.events.visits);
    final shared = _campaigns.fold<int>(0, (sum, c) => sum + c.events.shares);
    final saved = _campaigns.fold<int>(0, (sum, c) => sum + c.events.downloads);

    return Scaffold(
      backgroundColor: BrokaColors.bg,
      appBar: AppBar(
        backgroundColor: BrokaColors.bg,
        surfaceTintColor: Colors.transparent,
        foregroundColor: BrokaColors.textHigh,
        elevation: 0,
        title: const Text('Campaigns',
            style: TextStyle(
                fontSize: 16.5,
                fontWeight: FontWeight.w800,
                letterSpacing: -0.2)),
        actions: [
          IconButton(
            key: const Key('campaigns-new'),
            tooltip: 'New campaign',
            onPressed: _newCampaign,
            icon: const Icon(Icons.add_rounded, size: 22),
          ),
        ],
      ),
      body: RefreshIndicator(
        color: BrokaColors.gold,
        onRefresh: _load,
        child: ListView(
          key: const Key('marketing-campaigns'),
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            _card(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (_loading && _campaigns.isEmpty)
                    const Center(
                      child: Padding(
                        padding: EdgeInsets.symmetric(vertical: 8),
                        child: SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(
                              strokeWidth: 2, color: BrokaColors.gold),
                        ),
                      ),
                    )
                  else if (_error != null)
                    Column(
                      key: const Key('marketing-campaigns-error'),
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(_error!,
                            style: const TextStyle(
                                color: BrokaColors.textMid,
                                fontSize: 12.5,
                                height: 1.4)),
                        const SizedBox(height: 8),
                        OutlinedButton(
                            onPressed: _load, child: const Text('Try again')),
                      ],
                    )
                  else
                    Row(children: [
                      _CampaignStat(value: '$_total', label: 'Campaigns'),
                      _CampaignStat(
                          value: '$visits', label: 'Shop visits\nfrom links'),
                      _CampaignStat(
                          value: '${shared + saved}',
                          label: 'Posts shared\nor saved'),
                    ]),
                  const SizedBox(height: 9),
                  const Text(
                    'Only actions BROKA observed are counted. Impressions, '
                    'reach and ad spend are not measured, so they are not shown.',
                    style: TextStyle(
                        color: BrokaColors.textLow,
                        fontSize: 10.5,
                        height: 1.4),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            if (!_loading && _campaigns.isEmpty)
              _NoCampaigns(onStart: _newCampaign)
            else ...[
              for (final campaign in _campaigns)
                Padding(
                  padding: const EdgeInsets.only(bottom: 9),
                  child: _CampaignCard(
                      campaign: campaign, onTap: () => _open(campaign.id)),
                ),
              if (_total > _campaigns.length)
                OutlinedButton(
                  key: const Key('older-campaigns'),
                  onPressed: _showOlder,
                  style: OutlinedButton.styleFrom(
                    foregroundColor: BrokaColors.textHigh,
                    side: const BorderSide(color: BrokaColors.border),
                    minimumSize: const Size(0, 44),
                  ),
                  child: Text(
                      'Show older campaigns (${_total - _campaigns.length})'),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

class _NoCampaigns extends StatelessWidget {
  const _NoCampaigns({required this.onStart});
  final VoidCallback onStart;

  @override
  Widget build(BuildContext context) => _card(
        key: const Key('marketing-empty'),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('No campaigns yet',
              style: TextStyle(
                  color: BrokaColors.textHigh,
                  fontSize: 14,
                  fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          const Text(
            'Your first one takes about a minute and uses a product that is '
            'already live in your store.',
            style: TextStyle(
                color: BrokaColors.textMid, fontSize: 12, height: 1.4),
          ),
          const SizedBox(height: 12),
          const _CampaignStep(
              number: 1, text: 'Pick one live product and a platform.'),
          const _CampaignStep(
              number: 2, text: 'BROKA writes the caption and poster.'),
          const _CampaignStep(
              number: 3, text: 'Check it, then share it yourself.'),
          const SizedBox(height: 8),
          FilledButton.icon(
            key: const Key('empty-start-creative'),
            onPressed: onStart,
            icon: const Icon(Icons.add_rounded, size: 18),
            label: const Text('Create a campaign'),
            style: FilledButton.styleFrom(
              backgroundColor: _accent,
              foregroundColor: Colors.white,
              minimumSize: const Size(0, 44),
            ),
          ),
        ]),
      );
}

class _CampaignStep extends StatelessWidget {
  const _CampaignStep({required this.number, required this.text});
  final int number;
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Container(
            width: 19,
            height: 19,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: BrokaColors.gold.withOpacity(0.14),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text('$number',
                style: const TextStyle(
                    color: BrokaColors.gold,
                    fontSize: 10,
                    fontWeight: FontWeight.w700)),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.only(top: 1),
              child: Text(text,
                  style: const TextStyle(
                      color: BrokaColors.textMid, fontSize: 12, height: 1.35)),
            ),
          ),
        ]),
      );
}

class _CampaignStat extends StatelessWidget {
  const _CampaignStat({required this.value, required this.label});
  final String value;
  final String label;

  @override
  Widget build(BuildContext context) => Expanded(
        child: Column(children: [
          Text(value,
              style: const TextStyle(
                  color: BrokaColors.textHigh,
                  fontSize: 20,
                  fontWeight: FontWeight.w800)),
          const SizedBox(height: 2),
          Text(label,
              textAlign: TextAlign.center,
              style: const TextStyle(
                  color: BrokaColors.textMid, fontSize: 10.5, height: 1.25)),
        ]),
      );
}

class _CampaignCard extends StatelessWidget {
  const _CampaignCard({required this.campaign, required this.onTap});
  final MarketingCampaign campaign;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final platforms = campaign.platforms
        .map((p) => CampaignPlatform.parse(p)?.label ?? p)
        .join(' · ');
    return Material(
      color: BrokaColors.bgCard.withOpacity(0.6),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: BrokaColors.border.withOpacity(0.8)),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        key: Key('campaign-${campaign.id}'),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Expanded(
                  child: Text(campaign.titleOrObjective,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: BrokaColors.textHigh,
                          fontSize: 14.5,
                          fontWeight: FontWeight.w700))),
              const Icon(Icons.chevron_right_rounded,
                  color: BrokaColors.textMid),
            ]),
            const SizedBox(height: 4),
            Text(
                [platforms, _dateLabel(campaign.createdAt)]
                    .where((s) => s.isNotEmpty)
                    .join(' · '),
                style:
                    const TextStyle(color: BrokaColors.textMid, fontSize: 12)),
            const SizedBox(height: 8),
            Wrap(spacing: 10, runSpacing: 4, children: [
              _CampaignCount(
                icon: Icons.visibility_outlined,
                label: '${campaign.events.visits}',
                tooltip: 'Store visits from this campaign',
              ),
              _CampaignCount(
                icon: Icons.ios_share_rounded,
                label: '${campaign.events.shares}',
                tooltip: 'Times you shared a creative',
              ),
              if (campaign.products.isNotEmpty)
                _CampaignCount(
                  icon: Icons.inventory_2_outlined,
                  label: '${campaign.products.length}',
                  tooltip: 'Products promoted',
                ),
            ]),
          ]),
        ),
      ),
    );
  }
}

class _CampaignCount extends StatelessWidget {
  const _CampaignCount(
      {required this.icon, required this.label, required this.tooltip});
  final IconData icon;
  final String label;
  final String tooltip;

  @override
  Widget build(BuildContext context) => Tooltip(
        message: tooltip,
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 14, color: BrokaColors.textMid),
          const SizedBox(width: 4),
          Text(label,
              style: const TextStyle(
                  color: BrokaColors.textMid,
                  fontSize: 11.5,
                  fontWeight: FontWeight.w600)),
        ]),
      );
}
