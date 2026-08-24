import Home from "../../page";

// The desktop build loads the root SPA and performs client-side history
// navigation. Keeping one static placeholder lets Next export the route while
// preserving the existing browser mode.
export function generateStaticParams() {
  return [{ id: "desktop" }];
}

export default function WorkPage() {
  return <Home />;
}
