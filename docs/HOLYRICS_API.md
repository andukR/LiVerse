# Holyrics API documentation

Holyrics opens the JavaScript/API documentation through this internal link:

```text
https://www.holyrics.com.br/go.php?action=jslib_doc&lang=en
```

On 2026-07-06 this redirects to:

```text
https://github.com/holyrics/jslib/blob/main/README-en.md
```

Raw Markdown:

```text
https://raw.githubusercontent.com/holyrics/jslib/main/README-en.md
```

The document is large, about 15,600 lines at the time of checking.

Useful sections for LiVerse:

- `hly('GetAPIServerInfo')` - checks API Server state, port, and IP list.
- `hly('GetVersion')` - returns the running Holyrics program version, but it may require a separate token permission.
- `hly('GetTokenInfo')` - returns current token information. In local testing it returned both Holyrics version and enabled permissions.
- `hly('ShowVerse')` - starts a Bible verse presentation.
- `hly('SetBibleSettings')` - changes Bible module settings, including `show_x_verses`.
- `hly('GetThemes')` - returns saved themes. LiVerse does not ask the operator to choose or persist a theme; this call is only used where the active sermon-plan appearance must be reproduced.
- `hly('GetBibleVersionsV2')` - returns available Bible versions.

Practical LiVerse startup check:

1. Call `GetAPIServerInfo` to verify that Holyrics API Server is reachable.
2. Call `GetTokenInfo` to read Holyrics version and current token permissions.
3. Warn the user if `ShowVerse`, `SetBibleSettings`, or `GetAPIServerInfo` is missing.
4. LiVerse leaves theme selection to Holyrics and does not change `SetBibleSettings.theme`.
5. For ordinary Bible output, use the current Holyrics Bible-module settings. During a sermon-plan presentation, capture the active slide appearance only in memory for the temporary display and restoration; never reuse a saved theme name or ID from an earlier run.
