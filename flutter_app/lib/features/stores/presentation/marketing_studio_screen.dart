// BROKA — Marketing Studio, built around Zeno.
//
// The Studio is one conversation. Zeno is the seller's marketing partner: it
// reads this store's live products and the campaigns BROKA counted on the
// server, and answers in ordinary language. The seller types a question, gets
// an answer with real product facts, and can ask a follow-up ("make it
// shorter") without repeating the product.
//
//   _ZenoHeader / _StudioToolsRow   who is talking, and the three tools
//   _ZenoConversation               the transcript, composer and retry
//   _CampaignsScreen                saved campaigns and a new one
//   _CreativeStudioTab              poster, caption, video script
//   _AdsGuidanceTab                 the Meta Ads checklist
//   CampaignBuilderScreen           what to promote, then generate
//   CampaignDetailScreen            previews, edits, tracked links, counts
//
// The tools are pushed on top of the conversation, never swapped into it, so
// coming back keeps every turn and the seller's place in the thread.
//
// Boundaries the screens keep, because the backend does:
//   * Product facts and prices come from the server's copy of the seller's
//     listings; the app only names product ids.
//   * Nothing is posted or scheduled. A TikTok script is shown as a script.
//   * No screen shows reach, impressions, ad spend, sales or revenue: BROKA
//     does not measure them, and the counts shown are the events the server
//     actually recorded.
//   * Generating replaces this campaign's creatives. Editing a caption edits
//     the campaign, never the seller's listing.
//   * This conversation lives on this screen only. There is no saved
//     transcript, and the welcome says so rather than implying one.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/utils/result.dart';
import '../../../main.dart' show BrokaColors;
import '../../../widgets/broka_image.dart';
import '../data/creative_share.dart';
import '../data/repositories/stores_repository.dart';
import '../domain/models/marketing_sidekick.dart';
import '../domain/models/store.dart';
import '../domain/models/store_campaign.dart';
import '../domain/models/store_product.dart';

part 'marketing_studio_components.dart';
part 'marketing_zeno_welcome.dart';
part 'marketing_zeno_conversation.dart';
part 'marketing_zeno_message.dart';
part 'marketing_zeno_tools.dart';
part 'marketing_campaigns_screen.dart';
part 'marketing_creative_section.dart';
part 'marketing_ads_section.dart';
part 'marketing_platform_guide_sheet.dart';
part 'marketing_video_script_sheet.dart';
part 'marketing_campaign_builder.dart';
part 'marketing_campaign_detail.dart';

class MarketingStudioTab extends StatefulWidget {
  const MarketingStudioTab(
      {super.key, required this.store, required this.repo});
  final Store store;
  final StoresRepository repo;

  @override
  State<MarketingStudioTab> createState() => _MarketingStudioTabState();
}

class _MarketingStudioTabState extends State<MarketingStudioTab> {
  /// Bumping this rebuilds the conversation with a clean transcript. It is the
  /// "new conversation" action, not a saved-history feature: leaving the
  /// Studio ends the thread, and nothing pretends otherwise.
  int _session = 0;

  void _newConversation() => setState(() => _session++);

  void _openCreative([SidekickAction? action]) {
    _push(_StudioToolScreen(
      title: 'Creative Studio',
      subtitle: 'Make the post, then share it yourself',
      child: _CreativeStudioTab(
        store: widget.store,
        repo: widget.repo,
        initialProductId: action?.productId,
        initialPlatform: action?.platform,
        initialObjective: action?.objective,
        initialFormat: action?.creativeFormat,
      ),
    ));
  }

  void _openAds([SidekickAction? action]) {
    _push(_StudioToolScreen(
      title: 'Meta Ads',
      subtitle: 'A plan you finish in Ads Manager',
      child: _AdsGuidanceTab(
        store: widget.store,
        repo: widget.repo,
        initialProductId: action?.productId,
      ),
    ));
  }

  void _openCampaigns() {
    _push(_CampaignsScreen(store: widget.store, repo: widget.repo));
  }

  void _push(Widget screen) {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => screen));
  }

  /// A button Zeno offered, or one inside a tool. Each one only opens a real
  /// screen with the same context already chosen - nothing is created until
  /// the seller acts there.
  Future<void> _handleAction(SidekickAction action, BuildContext host) async {
    switch (action.type) {
      case 'creative_studio':
        _openCreative(action);
      case 'meta_ads':
        _openAds(action);
      case 'platform_guide':
        if (action.platform != null) {
          await showPlatformGuideSheet(
            host,
            repo: widget.repo,
            storeId: widget.store.id,
            platform: action.platform!,
            onAction: (next) => _handleAction(next, host),
          );
        }
      case 'video_script':
        await showVideoScriptSheet(
          host,
          repo: widget.repo,
          storeId: widget.store.id,
          initialPlatform: action.platform,
          initialProductId: action.productId,
        );
      default:
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const Key('marketing-studio'),
      children: [
        _ZenoHeader(store: widget.store, onNewConversation: _newConversation),
        _StudioToolsRow(
          onCreative: () => _openCreative(),
          onCampaigns: _openCampaigns,
          onAds: () => _openAds(),
        ),
        Expanded(
          child: _ZenoConversation(
            key: ValueKey('zeno-session-$_session'),
            store: widget.store,
            repo: widget.repo,
            onAction: (action) => _handleAction(action, context),
          ),
        ),
      ],
    );
  }
}

/// Who is talking: Zeno, for this store. One line, no card and no marketing
/// copy - the conversation below is the page.
class _ZenoHeader extends StatelessWidget {
  const _ZenoHeader({required this.store, required this.onNewConversation});
  final Store store;
  final VoidCallback onNewConversation;

  @override
  Widget build(BuildContext context) {
    final products = store.listingCount;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 10, 8, 6),
      child: Row(
        children: [
          const _ZenoMark(size: 40),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Zeno',
                    style: TextStyle(
                        color: BrokaColors.textHigh,
                        fontSize: 18,
                        height: 1.1,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -0.3)),
                const SizedBox(height: 2),
                Text(
                  '$products live product${products == 1 ? '' : 's'} · ${store.name}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      color: BrokaColors.textMid, fontSize: 11.5),
                ),
              ],
            ),
          ),
          IconButton(
            key: const Key('zeno-new-chat'),
            tooltip: 'New conversation',
            onPressed: onNewConversation,
            icon: const Icon(Icons.add_comment_outlined,
                size: 20, color: BrokaColors.textMid),
          ),
        ],
      ),
    );
  }
}
