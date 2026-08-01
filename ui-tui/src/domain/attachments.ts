import type { ComposerToken } from '../app/interfaces.js'
import { PASTE_SNIPPET_RE } from '../protocol/paste.js'

/**
 * Composer tokens are the ONE way deferred content shows up in the input line:
 * a collapsed paste and an attached image both render as `[[ … ]]` sitting in
 * the text the user is editing. They are ordinary characters — arrow keys,
 * backspace, and selection work on them for free — and they carry their real
 * payload out-of-band until submit.
 *
 * Two consequences the rest of the composer relies on:
 *   - Deleting the token is how you drop the thing. Nothing else to click.
 *   - Position in the text is meaningful: the model sees the payload where the
 *     token sat, not stapled to the front of the turn.
 */
export const imageToken = (index: number) => `[[ Image ${index} ]]`

/** Highest image token index handed out so far, so a new one never collides. */
export const nextImageIndex = (tokens: ComposerToken[]) =>
  tokens.reduce((max, t) => (t.kind === 'image' ? Math.max(max, t.index) : max), 0) + 1

/**
 * Tokens no longer backed by an occurrence in the composer text.
 *
 * Counts occurrences rather than testing set membership: repeated identical
 * labels are explicitly supported (see `expandTokens`), so deleting one of
 * three `[[ Image 1 ]]` tokens has to drop exactly one. Set membership would
 * see the label still present and drop none.
 *
 * Surviving tokens are matched to occurrences left to right, mirroring the
 * `shift()` order `expandTokens` expands in, so the extras dropped here are
 * the trailing ones.
 */
export const droppedTokens = (tokens: ComposerToken[], value: string) => {
  const remaining = new Map<string, number>()

  for (const label of value.match(PASTE_SNIPPET_RE) ?? []) {
    remaining.set(label, (remaining.get(label) ?? 0) + 1)
  }

  return tokens.filter(t => {
    const left = remaining.get(t.label) ?? 0

    if (left === 0) {
      return true
    }

    remaining.set(t.label, left - 1)

    return false
  })
}

/**
 * Resolve every token in `value` to what the agent should actually receive.
 *
 * Repeated identical labels expand in submission order (left to right), which
 * is why this walks matches instead of doing a global replace per token.
 *
 * An image token expands to nothing: the gateway already holds the file in
 * `session.attached_images` and splices the real vision content in at submit.
 * The token's job was to show the user where it landed, so it also eats one
 * adjacent space to avoid leaving a gap in the middle of a sentence.
 */
export const expandTokens = (tokens: ComposerToken[]) => {
  const byLabel = new Map<string, ComposerToken[]>()

  for (const token of tokens) {
    const hit = byLabel.get(token.label)
    hit ? hit.push(token) : byLabel.set(token.label, [token])
  }

  return (value: string) => {
    let expandedAny = false

    const expanded = value.replace(
      new RegExp(`[ \\t]?(?:${PASTE_SNIPPET_RE.source})`, 'g'),
      match => {
        const token = byLabel.get(match.trimStart())?.shift()

        if (!token) {
          return match
        }

        expandedAny = true

        return token.kind === 'paste' ? match.slice(0, match.length - token.label.length) + token.text : ''
      },
    )

    // Only trim when an expansion actually happened. The trim exists to clean
    // up the gap an image token leaves behind at the start or end of the
    // line — applying it unconditionally would silently rewrite token-free
    // input, so the text the agent receives would differ from the transcript
    // bubble for anyone who typed deliberate leading or trailing whitespace.
    return expandedAny ? expanded.trim() : expanded
  }
}
