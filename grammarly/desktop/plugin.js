import { COMPOSER_AREAS, host } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const MAX_INPUT_CHARS = 6000
const TEXT = navigator.language?.toLowerCase().startsWith('de')
  ? {
      emptyDraft: 'Das Eingabefeld ist leer.',
      tooLong: limit => `Bitte auf höchstens ${limit} Zeichen kürzen.`,
      checking: 'Grammarly prüft den Entwurf …',
      checkingNotice: 'Prüfe Rechtschreibung und Grammatik …',
      missingCorrection: 'Der Dienst hat keinen Korrekturvorschlag zurückgegeben.',
      changedDuringCheck: 'Der Entwurf wurde während der Prüfung geändert. Bitte erneut prüfen.',
      noCorrections: 'Keine Korrekturen gefunden.',
      failed: detail => `Prüfung fehlgeschlagen: ${detail}`,
      sessionChanged: 'Die Unterhaltung wurde gewechselt. Der Vorschlag wurde nicht übernommen.',
      sessionChangedNotice: 'Unterhaltung gewechselt; Vorschlag nicht übernommen.',
      staleDraft: 'Der Entwurf wurde geändert. Der veraltete Vorschlag wurde verworfen.',
      staleDraftNotice: 'Entwurf geändert; Vorschlag verworfen.',
      unavailableDraft: 'Das Eingabefeld ist nicht mehr verfügbar. Der Vorschlag wurde nicht übernommen.',
      unavailableDraftNotice: 'Eingabefeld nicht verfügbar; Vorschlag nicht übernommen.',
      applied: 'Korrektur übernommen.',
      checkingTitle: 'Grammarly prüft den Entwurf …',
      actionTitle: 'Grammarly: Rechtschreibung und Grammatik prüfen',
      checkingLabel: 'Grammarly prüft den Entwurf',
      actionLabel: 'Grammarly: Rechtschreibung und Grammatik prüfen',
      action: 'Korrektur prüfen',
      proposal: 'Grammarly-Korrekturvorschlag',
      review: 'Vorschlag prüfen — der Entwurf wird nicht automatisch geändert.',
      showOriginal: 'Original anzeigen',
      discard: 'Verwerfen',
      apply: 'Übernehmen'
    }
  : {
      emptyDraft: 'The draft is empty.',
      tooLong: limit => `Please shorten the draft to ${limit} characters or fewer.`,
      checking: 'Grammarly is checking the draft …',
      checkingNotice: 'Checking spelling and grammar …',
      missingCorrection: 'The service did not return a correction.',
      changedDuringCheck: 'The draft changed while it was being checked. Please try again.',
      noCorrections: 'No corrections found.',
      failed: detail => `Proofreading failed: ${detail}`,
      sessionChanged: 'The conversation changed. The suggestion was not applied.',
      sessionChangedNotice: 'Conversation changed; suggestion not applied.',
      staleDraft: 'The draft changed. The outdated suggestion was discarded.',
      staleDraftNotice: 'Draft changed; suggestion discarded.',
      unavailableDraft: 'The composer is no longer available. The suggestion was not applied.',
      unavailableDraftNotice: 'Composer unavailable; suggestion not applied.',
      applied: 'Correction applied.',
      checkingTitle: 'Grammarly is checking the draft …',
      actionTitle: 'Grammarly: Check spelling and grammar',
      checkingLabel: 'Grammarly is checking the draft',
      actionLabel: 'Grammarly: Check spelling and grammar',
      action: 'Proofread draft',
      proposal: 'Grammarly proofreading suggestion',
      review: 'Review the suggestion — your draft will not change automatically.',
      showOriginal: 'Show original',
      discard: 'Discard',
      apply: 'Apply'
    }

function ProofreadAction({ request }) {
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState(null)
  const [message, setMessage] = useState('')
  const proposalDialog = useRef(null)

  useEffect(() => {
    const dialog = proposalDialog.current
    if (!proposal || !dialog || dialog.open) return

    dialog.showModal()
    return () => {
      if (dialog.open) dialog.close()
    }
  }, [proposal])

  async function checkDraft() {
    setMessage('')
    setProposal(null)
    setBusy(true)

    try {
      const sessionId = host.state.focusedSessionId.get() || 'new'
      const original = await host.composer.getDraft(sessionId)
      if (typeof original !== 'string' || !original.trim()) {
        setMessage(TEXT.emptyDraft)
        host.notify({ kind: 'info', title: 'Grammarly', message: TEXT.emptyDraft })
        return
      }
      if (original.length > MAX_INPUT_CHARS) {
        const detail = TEXT.tooLong(MAX_INPUT_CHARS)
        setMessage(detail)
        host.notify({ kind: 'warning', title: 'Grammarly', message: detail })
        return
      }

      setMessage(TEXT.checking)
      host.notify({ kind: 'info', title: 'Grammarly', message: TEXT.checkingNotice })
      const result = await request('/proofread', {
        method: 'POST',
        body: { text: original },
        timeoutMs: 120_000
      })
      if (typeof result?.text !== 'string' || !result.text.trim()) {
        throw new Error(TEXT.missingCorrection)
      }

      const current = await host.composer.getDraft(sessionId)
      if (current !== original) {
        const detail = TEXT.changedDuringCheck
        setMessage(detail)
        host.notify({ kind: 'warning', title: 'Grammarly', message: detail })
        return
      }
      if (result.text === original) {
        setMessage(TEXT.noCorrections)
        host.notify({ kind: 'info', title: 'Grammarly', message: TEXT.noCorrections })
        return
      }

      setProposal({ sessionId, original, corrected: result.text })
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      setMessage(TEXT.failed(detail))
      host.notify({ kind: 'error', title: 'Grammarly', message: detail })
    } finally {
      setBusy(false)
    }
  }

  async function applyProposal() {
    if (!proposal) return

    try {
      const currentSessionId = host.state.focusedSessionId.get() || 'new'
      if (currentSessionId !== proposal.sessionId) {
        setMessage(TEXT.sessionChanged)
        host.notify({ kind: 'warning', title: 'Grammarly', message: TEXT.sessionChangedNotice })
        return
      }

      const current = await host.composer.getDraft(proposal.sessionId)
      if (current !== proposal.original) {
        setProposal(null)
        setMessage(TEXT.staleDraft)
        host.notify({ kind: 'warning', title: 'Grammarly', message: TEXT.staleDraftNotice })
        return
      }

      const applied = await host.composer.setDraft(proposal.sessionId, proposal.corrected)
      if (!applied) {
        setMessage(TEXT.unavailableDraft)
        host.notify({ kind: 'error', title: 'Grammarly', message: TEXT.unavailableDraftNotice })
        return
      }
      setProposal(null)
      setMessage(TEXT.applied)
      host.notify({ kind: 'success', title: 'Grammarly', message: TEXT.applied })
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      setMessage(`Übernahme fehlgeschlagen: ${detail}`)
      host.notify({ kind: 'error', title: 'Grammarly', message: detail })
    }
  }

  return jsxs('div', {
    className: 'relative flex items-center gap-1',
    children: [
      jsxs('button', {
        type: 'button',
        className:
          'inline-flex size-7 items-center justify-center rounded-md text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground disabled:cursor-wait disabled:opacity-60',
        title: busy ? TEXT.checkingTitle : TEXT.actionTitle,
        'aria-label': busy ? TEXT.checkingLabel : TEXT.actionLabel,
        'aria-busy': busy,
        disabled: busy,
        onClick: checkDraft,
        children: [
          jsx('svg', {
            viewBox: '0 0 24 24',
            fill: 'none',
            stroke: 'currentColor',
            strokeWidth: 1.8,
            strokeLinecap: 'round',
            strokeLinejoin: 'round',
            className: busy ? 'size-4 animate-spin' : 'size-4',
            'aria-hidden': 'true',
            children: busy
              ? jsx('path', { d: 'M12 3a9 9 0 1 0 9 9' })
              : jsxs('g', {
                  children: [
                    jsx('path', { d: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z' }),
                    jsx('path', { d: 'M19 14l1.1 2.9L23 18l-2.9 1.1L19 22l-1.1-2.9L15 18l2.9-1.1L19 14z' })
                  ]
                })
          }),
          jsx('span', { className: 'sr-only', children: TEXT.action })
        ]
      }),
      jsx('dialog', {
        ref: proposalDialog,
        onCancel: event => {
          event.preventDefault()
          setProposal(null)
        },
        className: 'rounded-xl border border-border bg-popover p-0 text-popover-foreground shadow-2xl',
        style: {
          width: 'min(34rem, calc(100vw - 2rem))',
          maxHeight: '80vh',
          maxWidth: 'none',
          overflow: 'auto',
          margin: 'auto',
          padding: 0
        },
        'aria-label': TEXT.proposal,
        children: proposal &&
          jsx('div', {
            className: 'flex flex-col gap-3 p-4',
            children: jsxs('div', {
              className: 'flex flex-col gap-2',
              children: [
                jsx('div', {
                  className: 'text-xs font-medium',
                  children: TEXT.review
                }),
                jsx('pre', {
                  className:
                    'max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-2 text-xs font-sans',
                  children: proposal.corrected
                }),
                jsxs('details', {
                  className: 'text-xs text-muted-foreground',
                  children: [
                    jsx('summary', {
                      className: 'cursor-pointer',
                      children: TEXT.showOriginal
                    }),
                    jsx('pre', {
                      className:
                        'mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-2 font-sans',
                      children: proposal.original
                    })
                  ]
                }),
                jsxs('div', {
                  className: 'flex justify-end gap-2',
                  children: [
                    jsx('button', {
                      type: 'button',
                      className: 'rounded-md px-2 py-1 text-xs hover:bg-muted',
                      onClick: () => setProposal(null),
                      children: TEXT.discard
                    }),
                    jsx('button', {
                      type: 'button',
                      className: 'rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground',
                      onClick: applyProposal,
                      children: TEXT.apply
                    })
                  ]
                })
              ]
            })
          })
        }),
      message &&
        jsx('span', {
          className: 'max-w-56 text-xs text-muted-foreground',
          role: 'status',
          'aria-live': 'polite',
          children: message
        })
    ]
  })
}

export default {
  id: 'grammarly',
  name: 'Grammarly',
  defaultEnabled: true,
  register(ctx) {
    ctx.register({
      id: 'proofread-action',
      area: COMPOSER_AREAS.actions,
      order: 90,
      render: () => jsx(ProofreadAction, { request: (path, options) => ctx.rest(path, options) })
    })
  }
}
