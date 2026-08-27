import "./globals.css";
import { Providers } from "./providers";

export const metadata = { title: "MIREYE · Physical-world intelligence", description: "Evidence-backed site and project intelligence." };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><Providers>{children}</Providers></body></html>;
}
