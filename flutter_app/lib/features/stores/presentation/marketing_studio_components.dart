part of 'marketing_studio_screen.dart';

const _accent = BrokaColors.neonPurple;

/// Zeno's mark: the little orb that heads every answer. A gradient rather
/// than an emoji or a stock icon, so the assistant has one identity across
/// the header, the welcome and each turn.
class _ZenoMark extends StatelessWidget {
  const _ZenoMark({this.size = 34});
  final double size;

  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          gradient: const LinearGradient(
            colors: [BrokaColors.gold, _accent],
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
          ),
          borderRadius: BorderRadius.circular(size * 0.34),
          boxShadow: [
            BoxShadow(
              color: _accent.withOpacity(0.28),
              blurRadius: size * 0.5,
              spreadRadius: -size * 0.15,
            ),
          ],
        ),
        child: Icon(Icons.auto_awesome_rounded,
            size: size * 0.48, color: Colors.white),
      );
}

Widget _card(
        {Key? key,
        required Widget child,
        EdgeInsets padding = const EdgeInsets.all(16)}) =>
    Container(
      key: key,
      padding: padding,
      decoration: BoxDecoration(
        color: BrokaColors.bgCard.withOpacity(0.6),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: BrokaColors.border.withOpacity(0.8)),
      ),
      child: child,
    );

Widget _sectionLabel(String text) => Padding(
      padding: const EdgeInsets.only(bottom: 10, left: 2),
      child: Text(text.toUpperCase(),
          style: const TextStyle(
              color: BrokaColors.textMid,
              fontSize: 11,
              fontWeight: FontWeight.w700,
              letterSpacing: 1.1)),
    );

String _dateLabel(DateTime? date) {
  if (date == null) return '';
  const months = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec'
  ];
  return '${date.day} ${months[date.month - 1]} ${date.year}';
}

Future<bool> _copy(BuildContext context, String text, String what) async {
  var copied = false;
  try {
    await Clipboard.setData(ClipboardData(text: text));
    copied = true;
  } catch (_) {
    // A clipboard write can fail on a locked device; the caller must not
    // report a copy that never happened.
  }
  if (!context.mounted) return copied;
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
        content: Text(copied ? '$what copied' : "Couldn't copy $what")));
  return copied;
}

Future<bool> _openExternal(String url) async {
  try {
    return await launchUrl(Uri.parse(url),
        mode: LaunchMode.externalApplication);
  } catch (_) {
    return false;
  }
}

/// Where each platform's post is written, once the seller has the copy. X
/// takes the text; WhatsApp takes it into a new message. The others open
/// their app/site - BROKA has no publishing permission there.
String _composeUrl(String platform, String text) {
  final encoded = Uri.encodeComponent(text);
  return switch (platform) {
    'whatsapp' => 'https://wa.me/?text=$encoded',
    'x' => 'https://x.com/intent/post?text=$encoded',
    'instagram' => 'https://www.instagram.com/',
    'tiktok' => 'https://www.tiktok.com/',
    'facebook' => 'https://www.facebook.com/',
    _ => 'https://broka.co.ke',
  };
}

/// The shape's name in the Creative Studio. The campaign builder has its own
/// copy of this wording for its form; both must read the same to the seller.
String _creativeShapeLabel(String format) => switch (format) {
      'message' => 'Chat message',
      'status' => 'Status',
      'caption' => 'Caption',
      'story' => 'Story',
      'carousel' => 'Carousel',
      'script' => 'Script',
      'video_plan' => 'Video plan',
      'post' => 'Post',
      _ => format,
    };
