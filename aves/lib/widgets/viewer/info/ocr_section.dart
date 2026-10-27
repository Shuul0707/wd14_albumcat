import 'package:aves/model/app_ocr.dart';
import 'package:aves/model/app_ocrs.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/model/settings/settings.dart';
import 'package:aves/theme/format.dart';
import 'package:aves/theme/icons.dart';
import 'package:aves/widgets/viewer/info/common.dart';
import 'package:material_ui/material_ui.dart';

/// The text OCR read off this photo, shown in the viewer's info page.
///
/// The text lived in the database with no way to look at it before: the search
/// box could tell you *which* photos contain a word, but not what a given photo
/// says. Here it is, selectable so it can be copied out.
///
/// A stored row with an empty [AppOcr.text] is shown too, as 「没有认出文字」.
/// That is the honest answer for most illustrations, and without it a photo
/// that was scanned and yielded nothing looks exactly like a photo nobody ever
/// scanned -- which is the one thing this section is supposed to distinguish.
class OcrTextSection extends StatelessWidget {
  final AvesEntry entry;

  const new({
    super.key,
    required this.entry,
  });

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appOcrs,
      builder: (context, child) {
        final ocr = appOcrs.of(entry.id);
        if (ocr == null) return const SizedBox();

        final text = ocr.text;
        return Column(
          crossAxisAlignment: .start,
          children: [
            const SectionRow(
              icon: AIcons.text,
              padding: EdgeInsets.only(top: 16, bottom: 8),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: text.isEmpty
                  ? Text(
                      '没有认出文字',
                      style: InfoRowGroup.keyStyle(context),
                    )
                  : SelectableText(
                      text,
                      style: InfoRowGroup.valueStyle,
                    ),
            ),
            Padding(
              padding: const EdgeInsets.only(left: 8, right: 8, top: 8),
              child: Text(
                _caption(context, ocr),
                style: InfoRowGroup.keyStyle(context),
              ),
            ),
          ],
        );
      },
    );
  }

  String _caption(BuildContext context, AppOcr ocr) {
    final date = DateTime.fromMillisecondsSinceEpoch(ocr.updated);
    return [
      if (ocr.engine.isNotEmpty) ocr.engine,
      if (ocr.hasText) '${ocr.lineCount} 行',
      if (ocr.updated > 0) formatDateTime(date, settings.avesLocale, MediaQuery.alwaysUse24HourFormatOf(context)),
    ].join(' · ');
  }
}
