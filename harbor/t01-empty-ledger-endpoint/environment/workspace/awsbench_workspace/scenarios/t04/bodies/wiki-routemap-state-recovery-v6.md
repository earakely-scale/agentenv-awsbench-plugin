# Routemap: state recovery

Owner: Platform (routemap).

1. `rmctl workflows pause all`
2. `rmctl state restore --latest-backup --in-place`
3. `rmctl publish --all`
4. `rmctl workflows resume all`
