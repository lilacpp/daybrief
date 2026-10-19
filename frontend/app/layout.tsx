import './globals.css';
export const metadata = {
  title: 'DayBrief — A clearer view of today',
  description: 'Three developments. Independent sources. One minute.',
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
