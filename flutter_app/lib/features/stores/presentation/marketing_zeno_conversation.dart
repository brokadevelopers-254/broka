part of 'marketing_studio_screen.dart';

// ── The conversation ────────────────────────────────────────────────────────
//
// One turn: the seller's words, a pending row, then Zeno's answer or a failed
// row with Retry. The transcript is held here for the session and sent back
// with each turn, so "the black leather one" has a subject. Nothing is written
// to the server as history: this Studio does not have a saved conversation.

class _ZenoEntry {
  _ZenoEntry.user(this.text)
      : role = 'user',
        reply = null,
        error = null,
        pending = false;
  _ZenoEntry.pending()
      : role = 'assistant',
        text = '',
        reply = null,
        error = null,
        pending = true;
  _ZenoEntry.answer(SidekickReply answer)
      : role = 'assistant',
        text = answer.reply,
        reply = answer,
        error = null,
        pending = false;
  _ZenoEntry.failed(this.error)
      : role = 'assistant',
        text = '',
        reply = null,
        pending = false;

  final String role;
  final String text;
  final SidekickReply? reply;
  final String? error;
  final bool pending;
}

class _ZenoConversation extends StatefulWidget {
  const _ZenoConversation({
    super.key,
    required this.store,
    required this.repo,
    required this.onAction,
  });
  final Store store;
  final StoresRepository repo;
  final ValueChanged<SidekickAction> onAction;

  @override
  State<_ZenoConversation> createState() => _ZenoConversationState();
}

class _ZenoConversationState extends State<_ZenoConversation> {
  final _controller = TextEditingController();
  final _scroll = ScrollController();
  final _focus = FocusNode();
  final List<_ZenoEntry> _entries = [];
  List<String> _suggestions = const [];
  bool _busy = false;
  String? _lastFailed;

  @override
  void dispose() {
    _controller.dispose();
    _scroll.dispose();
    _focus.dispose();
    super.dispose();
  }

  /// What the server gets as context: completed turns only, oldest first. The
  /// message being sent is separate, so it is never duplicated here.
  List<({String role, String text})> get _history {
    final turns = <({String role, String text})>[];
    for (final entry in _entries) {
      if (entry.pending || entry.error != null) continue;
      final text =
          entry.role == 'assistant' ? (entry.reply?.reply ?? '') : entry.text;
      if (text.trim().isNotEmpty) turns.add((role: entry.role, text: text));
    }
    return turns.length > 12 ? turns.sublist(turns.length - 12) : turns;
  }

  Future<void> _send([String? preset]) async {
    if (_busy) return;
    final text = (preset ?? _controller.text).trim();
    if (text.isEmpty) return;
    await _run(text, addUser: true);
  }

  Future<void> _run(String text, {required bool addUser}) async {
    if (_busy) return;
    final history = _history;
    setState(() {
      if (addUser) _entries.add(_ZenoEntry.user(text));
      _entries.add(_ZenoEntry.pending());
      _busy = true;
      _lastFailed = null;
      _suggestions = const [];
      _controller.clear();
    });
    _scrollToEnd();
    final result = await widget.repo.zenoTurn(
      widget.store.id,
      message: text,
      history: history,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (_entries.isNotEmpty && _entries.last.pending) _entries.removeLast();
      switch (result) {
        case Success(:final data):
          _entries.add(_ZenoEntry.answer(data));
          _suggestions = data.suggestions;
        case Failure(:final message):
          _entries.add(_ZenoEntry.failed(message));
          _lastFailed = text;
      }
    });
    _scrollToEnd();
  }

  Future<void> _retry() async {
    final text = _lastFailed;
    if (text == null || _busy) return;
    // Back to before the question that failed, then ask it again: one user
    // bubble for one question, however many times it is retried.
    setState(() {
      while (_entries.isNotEmpty && _entries.last.error != null) {
        _entries.removeLast();
      }
      if (_entries.isNotEmpty &&
          _entries.last.role == 'user' &&
          _entries.last.text == text) {
        _entries.removeLast();
      }
      _lastFailed = null;
    });
    await _run(text, addUser: true);
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(_scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 260), curve: Curves.easeOut);
    });
  }

  @override
  Widget build(BuildContext context) {
    final latestAnswer = _entries.reversed
        .where((entry) => entry.reply != null)
        .map((entry) => entry.reply!)
        .firstOrNull;
    final suggestions = latestAnswer == null
        ? const <String>[]
        : _suggestions.isNotEmpty
            ? _suggestions
            : latestAnswer.suggestions;

    return Column(
      key: const Key('zeno-conversation-pane'),
      children: [
        Expanded(
          child: ListView(
            key: const Key('zeno-conversation'),
            controller: _scroll,
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 18),
            children: [
              if (_entries.isEmpty)
                _ZenoWelcome(store: widget.store, onAsk: _send),
              for (var i = 0; i < _entries.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: _entryView(_entries[i], i),
                ),
              if (_entries.isNotEmpty && !_busy && suggestions.isNotEmpty)
                _ZenoNextSteps(
                  suggestions: suggestions.take(3).toList(),
                  onAsk: _send,
                ),
            ],
          ),
        ),
        _composer(),
      ],
    );
  }

  Widget _entryView(_ZenoEntry entry, int index) {
    if (entry.role == 'user') return _ZenoUserBubble(text: entry.text);
    if (entry.pending) return const _ZenoTyping();
    if (entry.error != null) {
      return _ZenoError(message: entry.error!, onRetry: _retry);
    }
    return _ZenoAnswer(
      reply: entry.reply!,
      index: index,
      onAction: widget.onAction,
    );
  }

  Widget _composer() {
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
        decoration: BoxDecoration(
          color: BrokaColors.bg.withOpacity(0.96),
          border: Border(
              top: BorderSide(color: BrokaColors.border.withOpacity(0.6))),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: Container(
                decoration: BoxDecoration(
                  color: BrokaColors.bgCard.withOpacity(0.75),
                  borderRadius: BorderRadius.circular(24),
                  border: Border.all(color: BrokaColors.border),
                ),
                child: TextField(
                  key: const Key('zeno-input'),
                  controller: _controller,
                  focusNode: _focus,
                  minLines: 1,
                  maxLines: 5,
                  textCapitalization: TextCapitalization.sentences,
                  scrollPadding: const EdgeInsets.only(bottom: 170),
                  textInputAction: TextInputAction.send,
                  onTapOutside: (_) =>
                      FocusManager.instance.primaryFocus?.unfocus(),
                  onSubmitted: (_) {
                    _send();
                    _focus.requestFocus();
                  },
                  style: const TextStyle(
                      color: BrokaColors.textHigh,
                      fontSize: 13.5,
                      height: 1.35),
                  decoration: const InputDecoration(
                    hintText: 'Ask Zeno about your store...',
                    hintStyle:
                        TextStyle(color: BrokaColors.textLow, fontSize: 13),
                    border: InputBorder.none,
                    isDense: true,
                    contentPadding:
                        EdgeInsets.symmetric(horizontal: 16, vertical: 13),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            SizedBox(
              width: 46,
              height: 46,
              child: Material(
                color: _accent,
                shape: const CircleBorder(),
                clipBehavior: Clip.antiAlias,
                child: InkWell(
                  key: const Key('zeno-send'),
                  onTap: _busy ? null : () => _send(),
                  child: Center(
                    child: _busy
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(
                                strokeWidth: 2, color: Colors.white))
                        : const Icon(Icons.arrow_upward_rounded,
                            size: 20, color: Colors.white),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The suggestions under the newest answer: Zeno's own next questions, which
/// keep the thread moving without a menu.
class _ZenoNextSteps extends StatelessWidget {
  const _ZenoNextSteps({required this.suggestions, required this.onAsk});
  final List<String> suggestions;
  final ValueChanged<String> onAsk;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6, top: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Padding(
            padding: EdgeInsets.only(left: 2, bottom: 6),
            child: Text('ASK NEXT',
                style: TextStyle(
                    color: BrokaColors.textLow,
                    fontSize: 9.5,
                    letterSpacing: 1,
                    fontWeight: FontWeight.w700)),
          ),
          for (final suggestion in suggestions)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: InkWell(
                key: Key('zeno-suggestion-${suggestion.hashCode}'),
                borderRadius: BorderRadius.circular(10),
                onTap: () => onAsk(suggestion),
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 2, vertical: 7),
                  child: Row(children: [
                    const Icon(Icons.north_east_rounded,
                        size: 12, color: _accent),
                    const SizedBox(width: 7),
                    Expanded(
                      child: Text(suggestion,
                          style: const TextStyle(
                              color: BrokaColors.textMid,
                              fontSize: 11.5,
                              height: 1.35)),
                    ),
                  ]),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
