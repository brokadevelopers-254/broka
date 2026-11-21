// The Marketing Studio is Zeno's conversation, driven through the real
// repository and fake transport.
//
// What must hold:
//   * the Studio opens on Zeno, with starters and the three tools, and no
//     marketing-guide wall;
//   * a question is sent, and the answer is rendered as blocks (lead, steps, a
//     pasteable draft, a next step) with the real product;
//   * a follow-up sends the earlier turns, so Zeno knows what "it" is;
//   * an action opens Creative Studio with the same product already chosen;
//   * a failed turn keeps the question and retries it once;
//   * starting a new conversation clears the thread;
//   * the whole experience fits a 320 px phone without overflowing.
import 'package:broka/features/stores/data/repositories/stores_repository.dart';
import 'package:broka/features/stores/domain/models/store.dart';
import 'package:broka/features/stores/presentation/marketing_studio_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_api.dart';

Map<String, dynamic> _storeJson() => {
      'id': 's1',
      'name': 'Clanix Electronics',
      'slug': 'clanix',
      'url': 'https://broka.co.ke/store/clanix',
      'category': 'Electronics',
      'is_active': true,
      'listing_count': 1,
    };

Map<String, dynamic> _productJson() => {
      'id': 'l1',
      'seller_id': 'u1',
      'name': 'Solar Lantern',
      'category': 'Electronics',
      'price': 2500.0,
      'price_unit': null,
      'lat': -1.0,
      'lng': 36.0,
      'location_name': 'Nairobi',
      'created_at': '2026-10-20T09:00:00',
      'status': 'active',
      'is_active': true,
      'store_state': 'live',
      'description': 'Brand new solar lantern.',
      'seller_name': 'Seller',
    };

Map<String, dynamic> _campaignJson() => {
      'id': 'c1',
      'title': 'Solar week',
      'objective': 'product',
      'tone': 'friendly',
      'language': 'en',
      'product_ids': ['l1'],
      'products': [
        {'id': 'l1', 'name': 'Solar Lantern', 'price': 2500.0},
      ],
      'platforms': ['instagram'],
      'status': 'generated',
      'link_token': 'tok1',
      'links': {
        'instagram': 'https://broka.co.ke/store/clanix?via=instagram&c=tok1',
      },
      'store_url': 'https://broka.co.ke/store/clanix?via=direct&c=tok1',
      'creatives': [
        {
          'id': 'cr1',
          'platform': 'instagram',
          'format': 'caption',
          'headline': 'Solar Lantern',
          'body': 'Solar Lantern is available at KES 2,500.',
          'hashtags': ['BROKA', 'Kenya'],
          'cta': 'Open the link',
          'media': {'aspect_ratio': '1:1'},
          'source': 'template',
        },
      ],
      'plan': const <Object?>[],
      'events': {
        'by_kind': const <String, int>{},
        'by_platform': const <String, int>{},
        'measured': const <String>[],
        'not_measured': const <String>[],
      },
      'generation': {'provider': 'template', 'fallback': true},
      'created_at': '2026-10-20T09:00:00',
      'updated_at': '2026-10-20T09:00:00',
    };

const _lead =
    'Start with the Solar Lantern at KES 2,500. It is live and has a clear '
    'photo, so it is the easiest one to promote first.';

Map<String, dynamic> _zenoJson() => {
      'reply': '$_lead\n\n'
          '1. Photograph it in daylight.\n'
          '2. Post it on Instagram with the real price.\n\n'
          'DRAFT:\nSolar Lantern - KES 2,500\nMessage us to order.\n\n'
          'NEXT: Open Creative Studio to make the poster.',
      'source': 'model',
      'provider': 'gemini',
      'available': true,
      'fallback_reason': null,
      'sections': [
        {'kind': 'text', 'title': '', 'text': _lead, 'items': <String>[]},
        {
          'kind': 'steps',
          'title': '',
          'text': '',
          'items': [
            'Photograph it in daylight.',
            'Post it on Instagram with the real price.',
          ],
        },
        {
          'kind': 'copy',
          'title': 'Ready to paste',
          'text': 'Solar Lantern - KES 2,500\nMessage us to order.',
          'items': <String>[],
        },
        {
          'kind': 'next',
          'title': 'Next step',
          'text': 'Open Creative Studio to make the poster.',
          'items': <String>[],
        },
      ],
      'platform': 'instagram',
      'products': [
        {
          'id': 'l1',
          'name': 'Solar Lantern',
          'price': 2500.0,
          'price_label': 'KES 2,500',
          'category': 'Electronics',
          'available_units': null,
        },
      ],
      'actions': [
        {
          'type': 'creative_studio',
          'label': 'Create the Instagram post',
          'description': 'Open Creative Studio with Solar Lantern selected.',
          'platform': 'instagram',
          'product_id': 'l1',
          'objective': 'product',
          'creative_format': 'caption',
        },
        {
          'type': 'platform_guide',
          'label': 'See the Instagram guide',
          'platform': 'instagram',
        },
        {
          'type': 'meta_ads',
          'label': 'Plan a Meta ad',
          'product_id': 'l1',
        },
      ],
      'suggestions': [
        'Should I use TikTok instead?',
        'How do I get repeat buyers?',
      ],
    };

bool _planHasBudget = true;
bool _failTurn = false;

Map<String, dynamic> _adsPlanJson() => {
      'product': {
        'id': 'l1',
        'name': 'Solar Lantern',
        'price': 2500.0,
        'price_label': 'KES 2,500',
      },
      'store': {'id': 's1', 'name': 'Clanix Electronics'},
      'currency': 'KES',
      'objective': 'traffic',
      'objective_label': 'Traffic',
      'objective_options': [
        {
          'value': 'traffic',
          'label': 'Traffic',
          'when': 'Send people to your storefront.'
        },
        {
          'value': 'sales',
          'label': 'Sales',
          'when': 'Send buyers to a product.'
        },
      ],
      'audience': 'People interested in Electronics',
      'location': 'Nairobi',
      'budget': _planHasBudget
          ? {
              'specified': true,
              'daily_kes': 750,
              'trial_days': 5,
              'total_kes': 3750,
              'note': 'This is the daily budget you chose, not a prediction.',
            }
          : {
              'specified': false,
              'daily_kes': null,
              'total_kes': null,
              'trial_days': 5,
              'note': 'No daily budget has been chosen.',
            },
      'tracking': {
        'website_purchases_measured': false,
        'setup_required': true,
        'summary': "BROKA's storefront does not send purchase events to Meta.",
        'steps': [
          'BROKA has no Meta Pixel or Conversions API connection.',
          'An order request or confirmed order is not a payment.',
        ],
      },
      'stages': [
        {
          'key': 'objective',
          'title': '1. Choose the objective',
          'summary': 'Recommended: Traffic.',
          'suggestion': 'Traffic',
          'steps': ['Open Meta Ads Manager and choose Create.'],
        },
      ],
      'manager_url':
          'https://adsmanager.facebook.com/adsmanager/manage/campaigns',
      'campaign_created': false,
      'disclaimer': 'BROKA has not created or published a campaign. This is a '
          'plan for you to complete in Meta Ads Manager. BROKA does not process '
          'buyer-to-seller payments.',
    };

bool _failCampaignCreation = false;

Object? _route(Uri uri) {
  final path = uri.path;
  if (path.endsWith('/manage/listings')) {
    return {
      'items': [_productJson()],
      'counts': {
        'all': 1,
        'live': 1,
        'hidden': 0,
        'in_deal': 0,
        'sold': 0,
        'archived': 0
      },
    };
  }
  if (path.endsWith('/marketing/zeno/turn')) {
    if (_failTurn) return const FakeResponse.error(statusCode: 503);
    return _zenoJson();
  }
  if (path.endsWith('/marketing/ads-plan')) return _adsPlanJson();
  if (path.endsWith('/marketing/campaigns')) {
    if (uri.queryParameters.containsKey('limit')) {
      return {'items': const <Object?>[], 'total': 0};
    }
    if (_failCampaignCreation) return const FakeResponse.error(statusCode: 500);
    return _campaignJson();
  }
  if (path.endsWith('/marketing/campaigns/c1')) return _campaignJson();
  return null;
}

void main() {
  setUpAll(installFakeApi);

  setUp(() {
    _failTurn = false;
    _failCampaignCreation = false;
    _planHasBudget = true;
    clearFakeRequests();
    setFakeRoute(_route);
  });

  Future<void> pumpStudio(WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: MarketingStudioTab(
          store: Store.fromJson(_storeJson()),
          repo: StoresRepository(),
        ),
      ),
    ));
    await tester.pumpAndSettle();
  }

  Future<void> ask(WidgetTester tester, String text) async {
    await tester.enterText(find.byKey(const Key('zeno-input')), text);
    await tester.tap(find.byKey(const Key('zeno-send')));
    await tester.pumpAndSettle();
  }

  testWidgets('the Studio opens on Zeno, not a guide', (tester) async {
    await pumpStudio(tester);

    expect(find.byKey(const Key('zeno-welcome')), findsOneWidget);
    expect(find.text("Hi, I'm Zeno."), findsOneWidget);
    expect(find.text('Your marketing partner at Clanix Electronics'),
        findsOneWidget);
    expect(find.byKey(const Key('zeno-conversation')), findsOneWidget);
    expect(find.byKey(const Key('zeno-input')), findsOneWidget);
    // The starters are the way in.
    expect(find.text('Which of my products should I promote first?'),
        findsOneWidget);
    expect(find.text('START A CONVERSATION'), findsOneWidget);
    // The old guide wall, its four sections and its AI Sidekick hero are gone.
    expect(find.text('AI Sidekick'), findsNothing);
    expect(find.byKey(const Key('marketing-section-overview')), findsNothing);
    expect(find.textContaining('Impressions, reach and ad'), findsNothing);
    // The three supporting tools are one tap away.
    for (final tool in const ['Creative', 'Campaigns', 'Meta Ads']) {
      expect(find.text(tool), findsOneWidget);
    }
  });

  testWidgets('an answer is structured and uses the real product',
      (tester) async {
    await pumpStudio(tester);
    await ask(tester, 'Which of my products should I promote first?');

    final sent = fakeRequests
        .firstWhere((r) => r.uri.path.endsWith('/marketing/zeno/turn'));
    expect((sent.json as Map)['message'],
        'Which of my products should I promote first?');
    expect((sent.json as Map)['history'], isEmpty);

    // A model answer is labelled as one, with the store's own product.
    expect(find.text('Your store + AI'), findsOneWidget);
    expect(find.textContaining('Start with the Solar Lantern'), findsOneWidget);
    expect(find.byKey(const Key('zeno-product-l1')), findsOneWidget);
    // Structure: steps, a pasteable draft and a next step.
    expect(find.text('Photograph it in daylight.'), findsOneWidget);
    expect(find.byKey(const Key('zeno-copy-block-1-2')), findsOneWidget);
    expect(find.textContaining('Solar Lantern - KES 2,500'), findsOneWidget);
    expect(find.textContaining('Open Creative Studio'), findsOneWidget);
    // Two ways to keep going.
    expect(find.text('ASK NEXT'), findsOneWidget);
  });

  testWidgets('a follow-up carries the earlier turns', (tester) async {
    await pumpStudio(tester);
    await ask(tester, 'Which of my products should I promote first?');
    await ask(tester, 'Make it shorter');

    final turns = fakeRequests
        .where((r) => r.uri.path.endsWith('/marketing/zeno/turn'))
        .toList();
    expect(turns, hasLength(2));
    final history = (turns.last.json as Map)['history'] as List;
    // The question and the answer, so "it" has a subject.
    expect(history[0]['role'], 'user');
    expect(history[0]['text'], 'Which of my products should I promote first?');
    expect(history[1]['role'], 'assistant');
    expect(history[1]['text'], contains('Solar Lantern'));
    expect((turns.last.json as Map)['message'], 'Make it shorter');
  });

  testWidgets('an action opens Creative Studio with the product chosen',
      (tester) async {
    await pumpStudio(tester);
    await ask(tester, 'Which of my products should I promote first?');

    final action =
        find.byKey(const Key('zeno-action-creative_studio-instagram'));
    await tester.ensureVisible(action);
    await tester.pumpAndSettle();
    await tester.tap(action);
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('marketing-creative')), findsOneWidget);
    expect(find.byKey(const Key('creative-product-l1')), findsOneWidget);
    expect(find.text('Generate creative'), findsOneWidget);

    // Back keeps the conversation, including the answer that offered it.
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('zeno-product-l1')), findsOneWidget);
  });

  testWidgets('each tool opens, and the conversation survives the trip',
      (tester) async {
    await pumpStudio(tester);

    await tester.tap(find.byKey(const Key('marketing-tool-creative')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('marketing-creative')), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('marketing-tool-campaigns')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('marketing-campaigns')), findsOneWidget);
    expect(find.text('Campaigns'), findsWidgets);
    await tester.pageBack();
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('marketing-tool-ads')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('marketing-ads')), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('zeno-welcome')), findsOneWidget);
  });

  testWidgets('a failed turn keeps the question and retries it once',
      (tester) async {
    _failTurn = true;
    await pumpStudio(tester);
    await ask(tester, 'How do I get more people to discover my store?');

    expect(find.byKey(const Key('zeno-error')), findsOneWidget);
    // The seller's own words are still there, and nothing was invented.
    expect(find.text('How do I get more people to discover my store?'),
        findsOneWidget);
    expect(find.text('Your store + AI'), findsNothing);

    _failTurn = false;
    await tester.tap(find.byKey(const Key('zeno-retry')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('zeno-error')), findsNothing);
    expect(find.textContaining('Start with the Solar Lantern'), findsOneWidget);
    // One question, retried once - not a second one appended to the thread.
    expect(
        fakeRequests
            .where((r) => r.uri.path.endsWith('/marketing/zeno/turn'))
            .length,
        2);
  });

  testWidgets('a new conversation clears the thread', (tester) async {
    await pumpStudio(tester);
    await ask(tester, 'Which of my products should I promote first?');
    expect(find.textContaining('Start with the Solar Lantern'), findsOneWidget);

    await tester.tap(find.byKey(const Key('zeno-new-chat')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('zeno-welcome')), findsOneWidget);
    expect(find.textContaining('Start with the Solar Lantern'), findsNothing);
  });

  testWidgets('generating a creative still sends the real choices',
      (tester) async {
    await pumpStudio(tester);
    await tester.tap(find.byKey(const Key('marketing-tool-creative')));
    await tester.pumpAndSettle();

    await tester
        .ensureVisible(find.byKey(const Key('creative-platform-instagram')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('creative-platform-instagram')));
    await tester.pumpAndSettle();

    await tester
        .ensureVisible(find.byKey(const Key('creative-format-caption')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('creative-format-caption')));
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.byKey(const Key('creative-generate')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('creative-generate')));
    await tester.pumpAndSettle();

    final post = fakeRequests.firstWhere((r) =>
        r.method == 'POST' && r.uri.path.endsWith('/marketing/campaigns'));
    final body = post.json as Map;
    expect(body['product_ids'], ['l1']);
    expect(body['platforms'], ['instagram']);
    expect(body['objective'], 'product');
    expect(body['creative_format'], 'caption');
    expect(body['generate'], true);

    expect(find.text('Campaign'), findsOneWidget);
    expect(find.textContaining('KES 2,500'), findsWidgets);
  });

  testWidgets('a failed generation keeps the choices and the words',
      (tester) async {
    _failCampaignCreation = true;
    await pumpStudio(tester);
    await tester.tap(find.byKey(const Key('marketing-tool-creative')));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('creative-instructions')),
        'Mention delivery to Kisumu');
    await tester.ensureVisible(find.byKey(const Key('creative-generate')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('creative-generate')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('creative-error')), findsOneWidget);
    expect(find.text('Mention delivery to Kisumu'), findsOneWidget);
    expect(find.byKey(const Key('creative-product-l1')), findsOneWidget);
    expect(find.text('Campaign'), findsNothing);
  });

  testWidgets('the Meta Ads plan is a checklist, not a created campaign',
      (tester) async {
    tester.view.physicalSize = const Size(430, 2200);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpStudio(tester);
    await tester.tap(find.byKey(const Key('marketing-tool-ads')));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('ads-budget')), '750');
    await tester.ensureVisible(find.byKey(const Key('ads-build-plan')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('ads-build-plan')));
    await tester.pumpAndSettle();

    final sent = fakeRequests
        .firstWhere((r) => r.uri.path.endsWith('/marketing/ads-plan'));
    expect((sent.json as Map)['budget_kes'], 750);
    expect((sent.json as Map)['product_id'], 'l1');

    expect(
        find.textContaining('BROKA has not created or published a campaign.'),
        findsOneWidget);
    expect(find.text('Open Meta Ads Manager'), findsOneWidget);
  });

  testWidgets('an unspecified budget is not turned into a recommendation',
      (tester) async {
    _planHasBudget = false;
    tester.view.physicalSize = const Size(430, 2200);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpStudio(tester);
    await tester.tap(find.byKey(const Key('marketing-tool-ads')));
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.byKey(const Key('ads-build-plan')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('ads-build-plan')));
    await tester.pumpAndSettle();

    final sent = fakeRequests
        .firstWhere((r) => r.uri.path.endsWith('/marketing/ads-plan'));
    expect((sent.json as Map).containsKey('budget_kes'), isFalse);

    expect(find.byKey(const Key('ads-budget-unset')), findsOneWidget);
    expect(find.text('Not selected yet'), findsOneWidget);
    expect(find.byKey(const Key('ads-budget-total')), findsNothing);
    expect(find.textContaining('KES 0'), findsNothing);
    expect(find.byKey(const Key('ads-tracking')), findsOneWidget);
    expect(find.text('Purchase tracking is not set up'), findsOneWidget);
  });

  testWidgets('the conversation fits a 320 px phone without overflowing',
      (tester) async {
    tester.view.physicalSize = const Size(320, 640);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpStudio(tester);
    expect(tester.takeException(), isNull);

    await ask(tester, 'Which of my products should I promote first?');
    expect(find.byKey(const Key('zeno-answer-1')), findsOneWidget);
    expect(find.textContaining('Start with the Solar Lantern'), findsOneWidget);
    // The composer stays reachable and nothing pushed the layout sideways.
    expect(find.byKey(const Key('zeno-input')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
