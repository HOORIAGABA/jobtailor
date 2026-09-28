/**
 * The measure for the résumé screens.
 *
 * It lives in a layout rather than in each page because the landing page at `/`
 * needs full-bleed bands and the app screens need a fixed measure, and the root
 * layout cannot give both. A layout per section is the App Router's own answer to
 * that, and it means no page has to remember to wrap itself.
 */
import { Page } from "@/components/ui";

export default function ResumesLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <Page>{children}</Page>;
}
