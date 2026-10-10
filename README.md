# GitHub Trending

A newspaper-styled digest of [GitHub Trending](https://github.com/trending). It gathers
repositories across JavaScript, TypeScript, Python, Java, Go, Rust, C and C++ for the daily,
weekly and monthly windows, plus the unfiltered overall trending list, and writes a short
Chinese summary of each repository's README. The result is a single static page — a language
dropdown and repo list on the left, repository cards on the right — rendered from plain JSON
under `data/`, with no backend and no build step.

A scheduled GitHub Actions workflow (`Update GitHub Trending`) refreshes the boards once a
day and commits any repository that has newly shown up on trending.
