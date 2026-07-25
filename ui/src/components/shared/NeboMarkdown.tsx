import Markdown from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import { useStore } from '@/store'

// react-markdown v10 runs a default `urlTransform` that strips hrefs whose
// scheme isn't on a safe allowlist — which would blank out our `nebo://` deep
// links before the custom <a> renderer ever sees them. Pass `nebo:` through
// explicitly; keep http(s)/mailto/tel and relative/anchor links, and blank any
// other scheme (so `javascript:` / `data:` stay neutralized).
function urlTransform(url: string): string {
  if (url.startsWith('nebo://')) return url
  if (/^(https?:|mailto:|tel:)/i.test(url)) return url
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return '' // some other scheme
  return url // relative / anchor / no scheme
}

/** Markdown renderer that turns `nebo://run/<id>[?step=<n>]` and
 *  `nebo://group/<path>` links into in-app navigation. All other markdown
 *  renders normally (external links open in a new tab). */
export function NeboMarkdown({ children }: { children: string }) {
  const navigateNebo = useStore(s => s.navigateNebo)
  return (
    <Markdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      urlTransform={urlTransform}
      components={{
        a({ href, children }) {
          if (href && href.startsWith('nebo://')) {
            return (
              <a
                href={href}
                className="text-primary underline decoration-dotted underline-offset-2 cursor-pointer"
                onClick={(e) => {
                  e.preventDefault()
                  navigateNebo(href)
                }}
              >
                {children}
              </a>
            )
          }
          return (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          )
        },
      }}
    >
      {children}
    </Markdown>
  )
}
