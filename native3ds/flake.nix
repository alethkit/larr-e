{
  description = "LARR-E Cam: native New 3DS camera app (devkitARM + libctru)";

  # devkitNix repackages devkitPro's official Docker images, so the toolchain
  # binaries are Linux ones: this builds on x86_64-linux and aarch64-linux
  # (on a Mac, inside a Linux VM such as OrbStack's; see README.md).
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    devkitNix.url = "github:bandithedoge/devkitNix";
    devkitNix.inputs.nixpkgs.follows = "nixpkgs";
    devkitNix.inputs.flake-utils.follows = "flake-utils";
  };

  outputs = { nixpkgs, flake-utils, devkitNix, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ devkitNix.overlays.default ];
        };
      in {
        # `nix develop`, then `make`.
        devShells.default = pkgs.mkShell.override { stdenv = pkgs.devkitNix.stdenvARM; } { };

        # `nix build` -> result/larre-cam.3dsx
        packages.default = pkgs.devkitNix.stdenvARM.mkDerivation {
          name = "larre-cam";
          src = ./.;
          installPhase = ''
            mkdir $out
            cp larre-cam.3dsx $out/
          '';
        };
      });
}
