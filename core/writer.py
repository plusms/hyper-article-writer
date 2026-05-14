import anthropic


def _gemini_call_messages(api_key: str, messages: list) -> str:
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        response = client.models.generate_content(model="gemini-2.0-flash", contents=contents)
        return response.text
    except Exception as e:
        return f"[生成失敗: {e}]"


WRITING_RULES = """
【ライティングルール（すべて厳守）】

■ 文体
- 必ず「です・ます」調で統一する
- 同じ語尾（〜ます・〜です）が3回以上連続しないよう体言止めを交える
- 推量（〜でしょう・〜かもしれません）は使わない。言い切りのみ

■ 文章構成
- PREP法：結論→理由・根拠→具体例→SoWhat（読者の次の判断・選択・行動）
- 各H3の末尾は必ずSoWhat。「〜重要です」「〜大切です」「〜しましょう」「〜確認してください」で終わらない→「〜することで△△できます」「〜になります」まで行動後の状態変化を書く
- SoWhatの文末バリエーション（同じ表現を2H3以上連続させない）：選択→「〜を選べます／絞り込めます」、判断→「〜かどうかを判断できます」、防止→「〜を防げます／リスクを抑えられます」、継続→「〜が続けやすくなります」、行動→「〜を試しやすくなります」、状態変化→「〜に変わります／〜できる環境が整います」
- H3の段落数は3〜5段落（<p>1つ・<table>1つ・<ul>/<ol>1つをそれぞれ1段落としてカウント）
- H3内にテキスト<p>を最低1つ入れる。テーブル・箇条書きのみはNG
- **1段落（<p>タグ）＝1トピック厳守。費用・アクセス・診療方法・特徴など複数トピックを1つの<p>に詰め込まない**
- **1段落は3文以内。4文以上になりそうなら2段落に分割するか箇条書きに変換する**
- 冒頭文は4ステップ：①読者の悩みを具体的に言語化（顕在悩み＋潜在リスク）→②誰向けか絞り込む→③記事で分かることを端的に示す→④読後の未来（「〜できます」「〜に変わります」まで書く。「〜を知りたい方へ」で終わらない）
- H2直下に導入文を入れる。3〜4文・80〜120文字を目安。最後は行動後の状態（「〜できるようになります」等）で締める。「〜を解説します」だけで終わるのはNG。H3・テーブル・リストで直接始めない
- 1つのH2内で、少なくとも1つのH3にテーブルまたは箇条書きを使用する

■ テーブルルール
- テーブルには必ず小見出し（題名）を直前の<p>または<div>で入れる
- テーブル一番左の列は<td>ではなく<th>を使う
- スクロールテーブル（4列以上）：<div class="scrolltable"><table>...</table></div>

■ 箇条書きルール
- 箇条書き（<ul>）には必ず小見出し（題名）を直前の<p>または<strong>で入れる
- 番号なし箇条書き（<ul><li>）はOK。番号付き「①②③」「(1)(2)(3)」は禁止

■ 禁止：記事内参照表現（一切使わない）
- 「以下の」「以上の」「下記の」「次の」「上記の」「前述の」「先ほど」
- 「次のセクション」「ページ上部」「記事冒頭」「後述」
- 「本記事」「この記事」「このブロック」
- 「〜で紹介します」「〜をご覧ください」「〜で解説します」「〜を確認してください」
- 「各院の詳細は後続の紹介ブロック」「後半のランキングで紹介」「以下のランキング」等の記事内の特定箇所を指す表現
- **「以下のとおり」は使用禁止**

■ 禁止：問いかけ表現
- 「〜に悩んでいませんか」「〜でお悩みではないですか」「〜ではないでしょうか」「〜に困っていませんか」

■ 禁止：テンプレ導入文
- 「この記事では」「今回は」「こちらでは」「ここでは」「本記事では」「ここからは」

■ 禁止：PREP接続語
- H3内のPREP展開で「つまり」「要するに」「例えば」を接続語として使わない
- 文章の流れ自体でPREP構造を表現する

■ H4タグ禁止
- <h4>タグは使用禁止。H3の下に詳細を入れたい場合は<p>で表現する

■ 絶対NGワード
- 抽象語：傾向・設計・前提・把握・整理・方向性・実態
- 結論をぼかす：現実的な選択肢になります・後悔しにくい・失敗しにくい・納得感のある
- テンプレ語：もちろん・大切です・なお・順番に・動線・生活動線・糸抜き（→抜糸）
- 不自然な強調：救世主・味方・第一歩・鍵・近道・スムーズ・最適・最もふさわしい
- 誰目線か不明：「実態」「現状」「現場では」

■ 絶対NGパターン（文頭・接続に使わない）
- 「多くの方が迷うのが〜」「〜と迷う方も少なくありません」「〜というケースが多く、」
- 「広告料金」
- 「以下のとおり」

■ 括弧・引用符ルール
- 文中の強調・固有名詞表記に「」（隅付き括弧）を使わない。強調は<strong>タグで表現する
- （）（丸括弧）は文意が変わらないと困る補足（例：シアリス（タダラフィル）、副作用（かゆみ・腫れ）等）のみ使用する。説明的な内容を（）で括るのは禁止

■ こそあど・指示語を使わない
- 「これ」「それ」「この」「その」「ここ」「そこ」→具体的な名詞に置き換える

■ 見出しルール
- H2・H3に「（）」「｜」「、」「【】」を使わない
- 使える記号：「！」「？」のみ。**「！」を使う場合、「！」の直前が意味のある語句になっていること（例：「ANS.！」のように「！」の前だけ切り取ると意味不明になる形はNG）**
- H2・H3に「徹底」という語を使わない
- まとめのH2見出しに「まとめ」という名称を使わない
- メインKWは前半のH2（1〜3番目）に必ず含める
- サブKWは前半〜中盤のH2に含める。各サブKWが最低1つのH2に入ること
- KWを含めた上で、そのKW・ジャンル・地域でしか通じない固有の表現にする
  NG：「費用について」「選び方のポイント」「注意点をご確認ください」
  OK：「ピコレーザー大阪の1回あたり費用と照射範囲の目安」
- H3見出しは「他の地域・他のジャンルに置き換えても成立する汎用表現」を禁止する
- **H3見出しにはメインKW・サブKWいずれか、または当該ジャンルの共起語（例：処方・副作用・配合・クリニック選び等）を自然な形で入れる。どのKWも入らない場合でも、そのジャンルに固有の語を最低1語含める**

■ 文章品質
- 主語を省略しない。「〜があります」「〜できます」は主語が不明なのでNG
- 「〜な場合があります」「〜も大切です」等の主張のない文を入れない
- 人間が使わない造語・不自然な表現を避ける（「総合的に最善な選択肢」「包括的なアプローチ」等）

■ 医療・美容系ルール
- 「絶対」「100%」「最強」「日本一」「最安値」は断定で使わない
- 自由診療の料金（税込）・回数・リスク・副作用を一箇所にまとめて記載する

■ クリニック紹介文
- ファクトチェック済みのデータのみ使用。補完・推測は一切しない
- [要確認]の項目はそのままの形でHTMLに出力し、本文に組み込まない
- **このクリニック固有のデータ・特徴のみ記述する。業界一般論・他院との比較背景・「〇〇が重要です」等の一般的な解説は一切書かない**
- **1段落＝1トピック（費用・アクセス・診療方法・実績などを1段落に混在させない）**
- **1段落3文以内、全体3〜4段落以内。料金は文章で列挙せずテーブルで整理する**
- 段落構成の目安：①費用・プラン概要（テーブル推奨）、②アクセス・診察方法、③このクリニックを選ぶ理由（固有の強みのみ）
- おすすめクリニック紹介のH2直下にはH3を置かない

■ 料金相場セクション・教育コンテンツH2でのクリニック名使用
- 料金相場セクション（費用・相場H2）の説明文中に具体的なクリニック名を出さない
- クリニック紹介ブロック以外のH2（選び方・費用・流れ等の教育コンテンツ系H2）でクリニック名を例示に使う場合は、訴求案件（最上位・推奨掲載院）以外のクリニック名を前面に出さない。他院を例示する場合は「一部のクリニックでは」等の匿名表現を使う
- 相場感・価格帯・料金を左右する要素の説明に集中する

■ まとめセクション
- <h2>タグで配置。見出しに「まとめ」という名称を使わない
- まとめの中に箇条書きを入れない。自然文で書く
"""

_QUALITY_BASE = """
【品質チェック基準（共通・全記事必須）】

■ NGワード・NGパターン（即修正）
Q1. こそあど言葉が使われていないか（これ・それ・この・その・ここ・そこ）
Q2. 記事内参照表現が使われていないか（以下の・以上の・下記の・次の・上記の・本記事・この記事・このブロック・先ほど・上記・前述の・次のセクション・ページ上部・〜で紹介します・〜をご覧ください・各院の詳細は後続の紹介ブロック・後半のランキングで紹介・以下のとおり）
Q3. 問いかけ表現が使われていないか（〜に悩んでいませんか・〜でお悩みではないですか・〜ではないでしょうか）
Q4. テンプレ導入文が使われていないか（この記事では・今回は・こちらでは・ここでは・本記事では・ここからは）
Q5. その他NGワード・NGパターンが使われていないか（傾向・設計・前提・把握・整理・方向性・もちろん・大切です・なお・順番に・動線・糸抜き・現実的な選択肢・後悔しにくい・失敗しにくい・スムーズ・活用・言えます・言えるでしょう・「判断につながります」「判断しやすくなります」等の間接的判断表現 ／ 「多くの方が迷うのが〜」「〜と迷う方も少なくありません」「〜というケースが多く、」「広告料金」「以下のとおり」）
Q5-a. 文中に「」（隅付き括弧）が使われていないか（強調は<strong>タグで表現する）
Q5-b. （）（丸括弧）が補足目的以外で使われていないか（薬品名・副作用名等の補足は許容。説明や解説の内容を括るのはNG）
Q6. 番号付き列挙（①②③等）が本文・見出しで使われていないか
Q7. H4タグが使われていないか（使用禁止）
Q8. 「つまり」「例えば」等の接続語をH3内のPREP接続に使っていないか（PREPは文章の流れで表現する）

■ 見出しルール
Q9. 見出し（H2・H3）に「（）」「｜」「、」「【】」が入っていないか
Q9-a. 見出し（H2・H3）に「徹底」という語が使われていないか
Q9-b. 「！」を使っている見出しで、「！」の直前が単語1語のみになっていないか（例：「ANS.！」「BEST！」等は内容が不明瞭なのでNG）
Q10. メインKWが前半のH2（1〜3番目）に含まれているか
Q11. 各サブKWが最低1つのH2に含まれているか
Q11-a. 各H3の見出しにメインKW・サブKW、またはジャンルの共起語が自然な形で入っているか
Q12. H3の見出しがそのKW・ジャンルでしか通じない固有の見出しか（「費用について」「選び方のポイント」「注意点をご確認ください」等の汎用表現はNG）

■ 構成・ブロックルール
Q13. H2直下（最初のH3の前）に導入文（3〜4文・80〜120文字）があるか。いきなりH3・テーブル・リストで始まっていないか。最後が「〜を解説します」だけで終わっていないか
Q14. 各H2に1つ以上テーブルまたは箇条書きが含まれているか
Q15. テーブル・箇条書きの直前に指示文（「次の表を確認してください」「以下で確認してください」等）が使われていないか
Q16. まとめがH2単位で設置されており「まとめ」という名称を見出しに使っていないか
Q17. 1つのH2が1つの疑問解決に集中しているか（途中で別の疑問・悩みを提示していないか）

■ H3本文ルール
Q18. 各H3が3段落以上5段落以内か（<p>タグ・<table>・<ul>/<ol>のそれぞれを1段落としてカウント）
Q19. H3内がテーブル・箇条書きのみになっていないか（テキスト段落が最低1つ必要）
Q20. 各H3の末尾にSoWhat（行動後の状態変化まで含む締め文）が入っているか。NG：「〜重要です」「〜大切です」（説明止まり）「〜しましょう」「〜確認してください」（行動喚起止まり）→OK：「〜することで△△できます」「〜が続けやすくなります」等
Q21. 各H3本文に「そのKWとジャンルでしか通じない文」が最低1文あるか（他ジャンル・他KWでも言える一般論のみで終わっていないか）

■ 冒頭文
Q22. 冒頭文がAFDE構成になっているか（A:読者の悩みを具体的に提示 / F:誰向けか絞り込む / D:記事で分かることを端的に示す / E:信頼性の根拠）
Q23. 冒頭文が潜在意図を言語化しているか（顕在意図の繰り返しではないか。「〜を知りたい方へ」で終わっていないか）

■ 文章品質
Q24. 主語が省略されていないか（「読者」「病院」等の主語もNG）
Q25. 削除しても意味の通じる文・主張のない短文が含まれていないか（「〜があります」「〜も大切です」「〜な場合があります」「詳しく解説します」等）
Q26. 人間が使わないような不自然な言葉・造語が使われていないか

■ HTML構造
Q27. まとめがdivボックスではなくh2タグで配置されているか
Q28. [要確認]箇所が本文に組み込まれず、そのまま記載されているか
"""

_QUALITY_LOCAL = """
【地域記事 追加チェック基準】

Q29. 各H3本文に「地域名を別地域に置換しても成立しない文」が1文以上あるか（地域固有情報の有無）
Q30. 主要サブエリア（駅名・エリア名）の粒度で書かれているか（「〇〇全体は〜」で止まっていないか）
Q31. 地域ユーザーの動機・不安・移動前提（交通手段・アクセス等）が反映されているか
Q32. 地域断言（「〇〇地域は〜がある」等）に根拠が示されているか（「傾向があります」で根拠なしはNG）
Q33. H2・H3の見出しが「地名を付け足しただけで本文が全国共通の内容」になっていないか（見出しに地域固有情報が反映されているか）
Q34. 費用相場セクションにその地域特有の価格帯コメントが入っているか
"""

_QUALITY_COMPARISON = """
【比較記事 追加チェック基準】

Q29. 比較軸が冒頭で明示されているか（「〇〇・〇〇・〇〇の3軸で比較します」等）
Q30. 比較情報がテーブルで整理されているか（文中に並べていないか）
Q31. 「向いている人・向いていない人」の条件が示されているか
Q32. 最終結論（「迷ったらこれ」相当の選択指針）が出ているか。結論なしで終わっていないか
Q33. 比較対象の選定根拠が示されているか（なぜその対象を選んだか）
"""

_QUALITY_BRAND = """
【商標記事 追加チェック基準】

Q29. ブランド名がタイトル・H1・冒頭文の前半に含まれているか
Q30. 費用総額（初診料・薬代・送料・追加費用の有無）が記載されているか
Q31. 診察の流れ（オンライン/対面の別）が記載されているか
Q32. 診療時間が記載されているか
Q33. 「怪しい」「副作用」「失敗」「効果なし」等ネガティブ系クエリへの対応が含まれているか
Q34. 冒頭に簡易料金表またはCTA（申込・予約・問い合わせ導線）が置かれているか
"""

_QUALITY_HOWTO = """
【ノウハウ記事 追加チェック基準】

Q29. クリニック紹介ブロック・具体的な料金表が設けられていないか（情報提供記事として）
Q30. 各H3が「どう判断・行動すればよいか」まで踏み込んでいるか（仕組み説明のみで終わっていないか）
Q31. CV記事誘導セクション（[要確認：関連CV記事URL]）がまとめの直前に置かれているか
Q32. 医療・専門用語をユーザーが行動判断できる粒度に噛み砕いているか
"""

_QUALITY_TYPE = {
    "地域": _QUALITY_LOCAL,
    "比較": _QUALITY_COMPARISON,
    "商標": _QUALITY_BRAND,
    "ノウハウ": _QUALITY_HOWTO,
}

_TYPE_INSTRUCTIONS = {
    "ノウハウ": """
【ノウハウ記事 執筆要件（厳守）】

■ 基本方針
- クリニック紹介ブロック・具体的な料金表は設けない
- 情報提供・解説に徹する。「〇〇できます」「〇〇で解決します」等の断定型で書く
- 「なぜそうなるか」の仕組みより「どう判断・行動すればよいか」を優先する
- 医学的・専門的な説明はユーザーが行動判断できる粒度まで噛み砕く
- 各H3でそのジャンル・KW特有の具体例・数値・条件を最低1つ入れる

■ CV記事誘導セクション（まとめの直前に配置・固定）
- <p>[要確認：関連CV記事のURLをここに挿入してください]</p> の形式でプレースホルダーを出力
- 誘導文は「〇〇で実際にクリニックを選ぶなら」「〇〇の費用を比較するなら」等のアクション起点で書く
- リンクは架空のURLを書かない。プレースホルダーのみ

■ ノウハウ禁止事項（CV記事との区別）
- 特定クリニックの紹介・評価はしない
- 「〇〇クリニックがおすすめ」「〇〇の料金は△△円」等の具体情報はNG
""",
    "地域": """
【地域記事 執筆要件（厳守）】

■ 地域固有情報
- 各H3本文に「地域名を別地域に置き換えると成立しない文」を最低1段落（3文以上）入れる
- 入れられないH3は削除するか別の地域固有トピックに差し替える（無理やり地名を挿入しない）
- 「どの地域でも同じことが言える一般論」に無理やり地名を入れて地域記事に見せかけない
- 主要サブエリア（駅名・エリア名）の粒度で記述する（「大阪全体では〜」で止めない）
- 地域ユーザーの移動前提・通院事情（最寄り駅からのアクセス・通いやすさ等）を反映する
- 地域断言には根拠を示す。「傾向があります」だけで根拠なしはNG

■ ノウハウの扱い（カニバリ防止）
- 以下はノウハウとして省く：「〇〇とは」「なぜ〇〇になるか」の仕組み解説・施術メカニズム・医学的説明・症状の種類や原因の説明
- 以下は地域記事として書く：クリニック選び方（地域・費用・アクセス軸）・料金費用相場・施術の流れ（予約〜アフターケア）・エリア・アクセス情報

■ 費用相場セクション
- そのジャンルで意思決定に必要な費用項目をすべて出す（プラン料金だけでなく、診察料・薬代・麻酔代・追加費用の有無等）
- 項目別 or 総額の選択はユーザーの納得感を基準にする
- その地域特有の価格帯コメントを1文入れる（「〇〇エリアでは〇万円台で選ぶと主要クリニックの価格帯に合いやすい」等。掲載院と著しく乖離する相場感を断定しない）

■ 施術重要軸の明示
- 施術ジャンルによって重要軸が異なる。複数回通院が前提の施術（脱毛・AGA・GLP-1等）は通いやすさ軸を優先する。短期完了型（シミ取り1回・二重術等）は価格軸を優先する
- ジャンルの特性を判断して「このエリアで選ぶ際の一番の判断軸」を文章中で明示する

■ CTAの配置
- 冒頭比較表にCTAを組み込む（比較表で意思決定熱量が上がるタイミングを逃さない）
- クリニック紹介ブロックの上位1〜3位の各クリニック紹介直下にCTAを置く（記事後半も含む）
- まとめの直前にも必ずCTAを設置する
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
""",
    "比較": """
【比較記事 執筆要件（厳守）】
- 冒頭（H1直後の冒頭比較表H2）に掲載院3〜5院のCV比較表を必ず設置する（料金・特徴・対象・アクセス等の軸で比較）
- 比較軸を冒頭で明示する（何と何を何で比べるかを最初に示す）
- 比較情報はテーブルで整理する（文中に並べない）
- 「向いている人・向いていない人」の条件を示す
- 最終結論（「迷ったらこれ」相当の選択指針）を出す。結論なしで終わらない
- 比較対象の選定根拠を示す

■ 費用相場セクション
- そのジャンルで意思決定に必要な費用項目をすべて出す（プラン料金だけでなく、診察料・薬代・麻酔代・追加費用の有無等）
- 項目別 or 総額の選択はユーザーの納得感を基準にする

■ CTAの配置
- クリニック紹介ブロックの上位1〜3位の各クリニック紹介直下にCTAを置く
- 冒頭比較表にもCTAを組み込む（比較表で意思決定熱量が上がるタイミングを逃さない）
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
""",
    "商標": """
【商標記事 執筆要件（厳守）】
- ブランド名をタイトル・H1・冒頭文の前半に含める
- 費用総額（初診料・薬代・送料・追加費用の有無）を記載する
- 診察の流れ（オンライン/対面の別）を記載する
- 診療時間を記載する
- ネガティブ系クエリ（怪しい・副作用・失敗・効果なし）への対応を含める
- 冒頭に簡易料金表またはCTA（申込・予約・問い合わせ導線）を置く

■ 口コミH2の構成（3H3が基本）
- H3①「好評口コミ」：ポジティブな口コミを箇条書きで提示 → 評価される理由・背景を本文で展開 → SoWhatは「〜と感じる方に向いています」等のベネフィット
- H3②「辛口口コミ」：ネガティブな口コミを箇条書きで提示 → 発生しやすい原因を示す → 対処法・解決策まで書いてSoWhatで締める（ネガティブで終わらない）
- H3③「口コミ総評」：好評・辛口を踏まえた総合評価 → 「どんな人に向いているか」で締める

■ CTAの配置
- ユーザーが「欲しい・試したい」と思うタイミング（料金確認直後・口コミ好評直後・まとめ直前等）に都度設置する
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
- 同じ文言を繰り返さない。H2ごとに訴求軸をずらす（料金訴求→口コミ訴求→安心訴求等）
""",
}


def _get_quality_criteria(article_type: str) -> str:
    return _QUALITY_BASE + _QUALITY_TYPE.get(article_type, "")


def _normalize_heading(line: str) -> str:
    """Normalize full-width colon and markdown bold in heading lines."""
    return line.replace("：", ":").replace("**", "").strip()


def _parse_h2_sections(structure_text: str) -> list[str]:
    """Extract H2 blocks (each H2 + its H3s) from structure_text."""
    sections = []
    current = []
    in_body = False
    for line in structure_text.split("\n"):
        norm = _normalize_heading(line)
        if norm.startswith("H1:"):
            in_body = True
            continue
        if not in_body:
            continue
        if norm == "---":
            break
        if norm.startswith("H2:"):
            if current:
                sections.append("\n".join(current))
            current = [norm]
        elif norm.startswith("H3:") and current:
            current.append("  " + norm)
    if current:
        sections.append("\n".join(current))
    return sections


def _build_body_prompt(
    inputs: dict,
    structure: dict,
    clinic_info: dict,
    competitor_analysis: dict | None,
    h2_scope: str,
    include_h1: bool,
    use_clinic_placeholder: bool = False,
    site_parts: str = "",
) -> str:
    article_type = inputs["article_type"]
    clinic_names = list(clinic_info.keys()) if clinic_info else []
    clinic_info_text = "\n\n".join(
        f"【{name}】\n{info}" for name, info in clinic_info.items()
    )

    if article_type == "地域":
        type_context = f"ジャンル: {inputs['genre']}（地域名はメインKWから自動判断）"
    elif article_type == "比較":
        type_context = f"ジャンル: {inputs['genre']}"
    else:  # 商標
        _trademark_clinic = clinic_names[0] if clinic_names else "（メインKWから判断）"
        type_context = (
            f"ジャンル: {inputs['genre']}\n"
            f"【商標記事・1院専用】掲載クリニック：{_trademark_clinic}\n"
            "この記事は上記1院専用。他院との比較・他院名の言及・複数院前提の表現は一切しない。"
        )

    recommended_note = (
        f"【最訴求プラン】{inputs['recommended']}\n"
        "【配置ルール（厳守）】\n"
        "- 配置OK：おすすめクリニック紹介H2 → 最上位に配置する\n"
        "- 配置OK：費用相場・まとめ → 文脈上自然であれば院名・プラン名を出してよい\n"
        "- 配置NG：仕組み解説・選び方・症状説明・知識教育系セクション\n"
        "  このフェーズのユーザーはまだ「始めるかどうか」を検討中。\n"
        "  院名・プランを出すと広告感になり信頼を損なう。\n"
        "  教育系コンテンツで治療を始める動機を固め、選択肢の提示はクリニック紹介ブロックで行う。\n"
    ) if inputs.get("recommended") else ""

    appeal_note = ""
    _appeals = [a for a in inputs.get("appeal_points", []) if a and a.strip()]
    if _appeals:
        if article_type == "商標":
            appeal_note = "【比較優位性・強み（厳守）】\n"
            appeal_note += "以下の強みを記事全体に自然に散りばめる。専用セクションは不要。読者が「なぜここがいいのか」を自然に理解できるよう組み込む。\n"
            for i, ap in enumerate(_appeals, 1):
                appeal_note += f"強み{i}: {ap}\n"
            appeal_note += "※強みの表現を露骨に列挙しない。各H3の文脈に自然に溶け込ませる。根拠がある場合はその根拠も本文に反映する。\n"
        else:
            appeal_note = "【訴求インプット（優先度順）】\n"
            for i, ap in enumerate(_appeals, 1):
                appeal_note += f"第{i}訴求: {ap}\n"
            appeal_note += "※第1訴求を最も強調した表現で本文に反映する。専用H2は不要、各H3の文脈に自然に組み込む。\n"

    user_awareness_note = ""
    if inputs.get("user_awareness", "").strip():
        user_awareness_note = f"【ユーザーの前提・認識レベル】\n{inputs['user_awareness']}\n※この認識状態に合わせて説明の深さ・切り口・トーンを調整する。\n"

    custom_intent_note = ""
    if inputs.get("custom_intent", "").strip():
        custom_intent_note = f"【追加指示の意図・切り口】\n{inputs['custom_intent']}\n※追加指示をこの意図・切り口で本文に組み込む。\n"

    custom_block_note = (
        f"【追加指示（厳守）】\n{inputs['custom_block']}\n"
        "※この指示を本文全体に反映すること。禁止事項は一切言及・記述しない。\n"
    ) if inputs.get("custom_block", "").strip() else ""

    _clinic_count = inputs.get("clinic_count", 0)
    if _clinic_count > 0:
        _count_instruction = f"掲載院数：{_clinic_count}院（紹介H2内のH3をちょうど{_clinic_count}個にすること）\n"
    else:
        _count_instruction = "掲載院数：競合の掲載院数に合わせた適切な数（多すぎず少なすぎず）\n"

    if article_type == "商標":
        # 商標記事は院数にかかわらず常に1院専用ルールを適用（app.py側で1院に絞られているが念のため）
        _trademark_name = clinic_names[0] if clinic_names else "（メインKWから判断）"
        clinic_restriction = (
            f"【掲載クリニック（1院専用・厳守）】\n"
            f"掲載クリニック：{_trademark_name}\n"
            "記事全体を通じてこの1院に絞った情報のみ記載する。\n"
            "他院との比較・他院名の言及・複数院前提の表現（「各クリニック」「おすすめの院」「他のクリニック」等）は一切使わない。\n"
            "クリニック紹介のH2・H3は対象院のみ。「おすすめクリニック紹介」のような複数院を示唆する見出しも使わない。\n"
        )
    elif clinic_names:
        clinic_restriction = (
            f"【クリニック紹介の制約】\n"
            f"{_count_instruction}"
            f"紹介できるクリニック：{', '.join(clinic_names)}\n"
            "このリスト以外のクリニックは紹介しない。[要確認]としても出力しない。\n"
        )
    else:
        clinic_restriction = "クリニック指定なし：クリニック紹介セクションは設けない。\n"

    clinic_context_note = ""
    _clinics_with_context = [c for c in inputs.get("clinics", []) if c.get("recommended") or c.get("appeal")]
    if _clinics_with_context:
        clinic_context_note = "【案件別 訴求情報】\n"
        for c in inputs.get("clinics", []):
            if c.get("recommended") or c.get("appeal"):
                clinic_context_note += f"■ {c['name']}\n"
                if c.get("recommended"):
                    clinic_context_note += f"  最訴求プラン: {c['recommended']}\n"
                if c.get("appeal"):
                    clinic_context_note += f"  強み・比較優位性: {c['appeal']}\n"

    competitor_note = (
        f"【競合分析サマリー（差別化・網羅性チェックに使用）】\n"
        f"{competitor_analysis.get('analysis', '')[:2000]}\n"
    ) if competitor_analysis else ""

    type_instruction = _TYPE_INSTRUCTIONS.get(article_type, "")

    scope_note = (
        "H1と冒頭文から始め、【今回生成するH2】に列挙したH2セクションのみを出力してください。"
        if include_h1 else
        "【今回生成するH2】に列挙したH2セクションのみを出力してください。H1・冒頭文は出力しない。"
    )

    todo_note = (
        "最後に「---[要確認]リスト---」として取得できなかった項目を箇条書きでまとめる。"
        if not include_h1 else ""
    )

    clinic_placeholder_note = (
        "\n- おすすめクリニック紹介のH2セクション（「おすすめ」「紹介」「ランキング」等を含むH2）は、"
        "本文を一切生成しない。<h2>タグのみ出力し、直後に `<!-- クリニック紹介ブロック入る -->` とだけ記述して次のH2に進む。\n"
    ) if use_clinic_placeholder else ""

    site_parts_block = (
        f"\n{site_parts}\n"
        "【サイトパーツ使用ルール（厳守）】\n"
        "- 「H2」パーツ → すべてのH2見出しに使用（裸の<h2>タグ禁止）\n"
        "- 「H3」パーツ → すべてのH3見出しに使用（裸の<h3>タグ禁止）\n"
        "- 「小見出し」パーツ → テーブル・箇条書きの直前に必ず使用（<p>や<strong>で代替しない）\n"
        "- 「箇条書き（リスト）」「箇条書き（チェックリスト）」「箇条書き（数字）」パーツ → 対応するリストに使用（裸の<ul><li>タグ禁止）\n"
        "- 「ボックス①（枠のみ）」「ボックス②（背景色あり）」パーツ → 箇条書き全体をこのパーツで包む\n"
        "- 「マーカー」パーツ → 各H3で最重要フレーズを1か所のみ。サイトパーツのHTML形式を使う（<span class=\"marker\">等の汎用タグ禁止）\n"
        "- 「太文字」パーツ → 各H3でサブ主張・強調箇所を1〜2か所。サイトパーツのHTML形式を使う（<span class=\"b\">等の汎用タグ禁止）\n"
        "- {{変数名}}は実際の内容に置き換えること。パーツ一覧にないクラス名・タグは使用しない\n"
    ) if site_parts else ""

    # HTML出力ルール：サイトパーツがある場合はパーツ参照に切り替え（汎用タグ指定と矛盾しないよう）
    if site_parts:
        _html_heading = "- 見出し(H1): <h1> タグを使用\n- 見出し(H2/H3): 上記「サイトパーツ使用ルール」の「H2」「H3」パーツのHTML形式を使用（裸の<h2><h3>タグ禁止）"
        _html_marker  = '- マーカー: 上記「マーカー」サイトパーツのHTML形式を使用（<span class="marker">等の汎用タグ禁止）。各H3に1か所のみ。核心フレーズのみ囲む（一文まるごと・単語1語だけは禁止）'
        _html_bold    = '- 太文字: 上記「太文字」サイトパーツのHTML形式を使用（<span class="b">等の汎用タグ禁止）。各H3に1〜2か所。強調したい語句を囲む'
    else:
        _html_heading = "- 見出し: <h1>、<h2>、<h3> タグを使用（<h4>は禁止）"
        _html_marker  = '- マーカー: <span class="marker">テキスト</span>（各H3に1か所のみ。そのH3で最も伝えたい主張の核心フレーズを囲む。意味が伝わる最小範囲にする。一文まるごとを囲まない。単語1語のみを囲まない）'
        _html_bold    = '- 太文字: <span class="b">テキスト</span>（各H3に1〜2か所。サブ的な主張・強調したい語句を囲む）'

    return f"""あなたはSEO記事の執筆専門家です。
記事全体の構成を把握したうえで、指定されたH2セクションのみHTMLで出力してください。

{scope_note}

【記事タイプ】{article_type}
【{type_context}】
【メインKW】{inputs['main_kw']}
【サブKW】{', '.join(inputs['sub_kw'])}
{custom_block_note}{recommended_note}{appeal_note}{user_awareness_note}{custom_intent_note}{clinic_context_note}
{clinic_restriction}
{competitor_note}
【記事全体の構成（把握用）】
{structure['structure_text'][:3000]}

【今回生成するH2】
{h2_scope}

【クリニック情報（このデータのみ使用・補完・推測禁止）】
{clinic_info_text[:12000] if clinic_info_text else "（情報なし）"}

{WRITING_RULES}
{type_instruction}
{site_parts_block}
【HTML出力ルール】
{_html_heading}
- 段落: <p> タグ（裸のテキスト禁止）
{_html_marker}
{_html_bold}
- 比較表: <table class="comparison-table"><thead><tr><th>...</th></tr></thead><tbody>...</tbody></table>
- [要確認]箇所: テキストをそのまま出力する（例: <p>[要確認：GoogleマップID]</p>）。補完しない
- HTMLの外側にコードブロック記号（```）をつけない。HTMLをそのまま出力する
- 各H2セクションの先頭（H2見出しタグの直前）に必ず `<!-- H2_BLOCK_START:{{H2の見出しテキスト}} -->` を1行挿入する（H3には挿入しない）{clinic_placeholder_note}
{todo_note}
【出力前の自己チェック（必ず実行・チェック結果は出力しない）】
以下を確認し、違反があれば修正してからHTMLのみを出力してください。
① こそあど言葉（これ・それ・この・その・ここ・そこ）→ 具体的な名詞に置き換える
② 各H3の末尾がSoWhat（行動後の状態変化まで書いた締め文）になっているか
   NG：「〜重要です」「〜大切です」「〜しましょう」「〜確認してください」
   OK：「〜することで△△できます」「〜が続けやすくなります」等
③ H2直下に導入文（3〜4文・80〜120文字）があるか。いきなりH3・テーブル・リストで始まっていないか
④ 記事内参照表現（以下の・上記の・本記事・先ほど・前述・次のセクション等）が使われていないか
⑤ SoWhatの文末パターンが連続2H3以上重複していないか
⑥ メインKW「{inputs['main_kw']}」が今回生成するH2のいずれかに含まれているか（前半担当のターンの場合は必須）。含まれていなければ見出し文言を修正する
⑦ サブKW（{', '.join(inputs['sub_kw'])}）それぞれが最低1つのH2に含まれているか。未含有のサブKWがあれば対応H2の文言を修正する
⑧ H2・H3に「（）」「｜」「、」「【】」が使われていないか
⑨ 1段落（<p>）に複数トピックが詰め込まれていないか。4文以上の段落があれば分割または箇条書きに変換する
⑩ クリニック紹介文に業界一般論が混入していないか（「〇〇が重要です」「インターネット通販のリスク」等の一般解説は削除する）"""


def generate_body(
    inputs: dict,
    structure: dict,
    clinic_info: dict,
    claude_api_key: str,
    competitor_analysis: dict | None = None,
    site_parts: str = "",
    gemini_api_key: str = "",
    article_provider: str = "claude",
) -> dict:
    use_clinic_placeholder = inputs.get("article_type") in ("地域", "比較")

    def _call(messages: list) -> str:
        if article_provider == "gemini" and gemini_api_key:
            return _gemini_call_messages(gemini_api_key, messages)
        client = anthropic.Anthropic(api_key=claude_api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=messages,
        )
        return msg.content[0].text

    def _finish(raw: str, debug: str = "") -> dict:
        html_part = raw
        todo_part = ""
        if "---[要確認]リスト---" in raw:
            parts = raw.split("---[要確認]リスト---", 1)
            html_part = parts[0].strip()
            todo_part = parts[1].strip()
        result = {"html": html_part, "todo_list": todo_part}
        if debug:
            result["debug"] = debug
        return result

    h2_sections = _parse_h2_sections(structure["structure_text"])

    if not h2_sections:
        # H2パース失敗: 構成テキスト全体をスコープとして単発コール
        fallback_prompt = _build_body_prompt(
            inputs, structure, clinic_info, competitor_analysis,
            h2_scope=structure["structure_text"],
            include_h1=True,
            use_clinic_placeholder=use_clinic_placeholder,
            site_parts=site_parts,
        )
        return _finish(_call([{"role": "user", "content": fallback_prompt}]),
                       debug="H2パース失敗: フォールバック使用")

    mid = max(1, len(h2_sections) // 2)
    first_half = h2_sections[:mid]
    second_half = h2_sections[mid:]

    # ターン1: H1 + 冒頭文 + 前半H2（自己チェック内包）
    prompt1 = _build_body_prompt(
        inputs, structure, clinic_info, competitor_analysis,
        h2_scope="\n".join(first_half),
        include_h1=True,
        use_clinic_placeholder=use_clinic_placeholder,
        site_parts=site_parts,
    )
    messages = [{"role": "user", "content": prompt1}]
    part1 = _call(messages)

    if not second_half:
        return _finish(part1)

    # ターン2: 後半H2（前半の会話コンテキストを保持した状態で生成・自己チェック内包）
    messages.append({"role": "assistant", "content": part1})
    prompt2 = _build_body_prompt(
        inputs, structure, clinic_info, competitor_analysis,
        h2_scope="\n".join(second_half),
        include_h1=False,
        use_clinic_placeholder=use_clinic_placeholder,
        site_parts=site_parts,
    )
    messages.append({"role": "user", "content": prompt2})
    part2 = _call(messages)

    raw = part1.rstrip() + "\n" + part2.lstrip()
    return _finish(raw)


def quality_check(html: str, article_type: str, main_kw: str, sub_kw: list, claude_api_key: str, gemini_api_key: str = "", article_provider: str = "claude", check_mode: str = "standard") -> str:
    def _call_llm(p: str) -> str:
        if article_provider == "gemini" and gemini_api_key:
            return _gemini_call_messages(gemini_api_key, [{"role": "user", "content": p}])
        client = anthropic.Anthropic(api_key=claude_api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4096,
            messages=[{"role": "user", "content": p}],
        )
        return msg.content[0].text

    if check_mode == "reader_rejection":
        prompt = f"""以下の記事HTML（{article_type}記事）を読んだユーザーが「この記事で最も推奨されているクリニック（または選択肢）を選ばない理由」を、読者目線で徹底的に洗い出してください。

【メインKW】{main_kw}
【サブKW】{', '.join(sub_kw)}

【記事HTML】
{html[:8000]}

些細な点も含め、なるべく多く・具体的に出してください。

## 出力形式

### 読者が選ばない理由
| # | 理由（読者目線での具体的な不安・疑問・不満） | 該当箇所（セクション名またはテキスト引用） |
|---|------------------------------------------|----------------------------------------|
| 1 | ... | ... |

### 修正提案
| # | 修正方針（何をどう書き換えるか・どのセクションに何を追加するか） |
|---|---------------------------------------------------------------|
| 1 | ... |
"""
        return _call_llm(prompt)

    criteria = _get_quality_criteria(article_type)

    prompt = f"""以下の記事HTML（{article_type}記事）の品質チェックを実施してください。

【メインKW】{main_kw}
【サブKW】{', '.join(sub_kw)}

{criteria}

【記事HTML】
{html[:6000]}

チェック結果を以下の形式で出力してください。
全項目に対してひとつずつ判定を行い、スキップしないこと。

## 品質チェック結果（{article_type}記事）

### ❌ 要修正
| 項目番号 | 問題箇所（該当テキスト or タグ） | 修正指示（具体的に） |
|---------|-------------------------------|-------------------|
| Q〇 | （該当箇所を引用） | （どう直すか） |

### ⚠️ 要確認
| 項目番号 | 該当箇所 | 確認内容 |
|---------|---------|---------|
| Q〇 | （該当箇所） | （何を確認するか） |

### ✅ 問題なし
Q〇, Q〇, Q〇 …（問題なしの項目番号を列挙）
"""

    return _call_llm(prompt)
