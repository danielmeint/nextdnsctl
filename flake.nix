{
  description = "Bulk-edit, import and back up NextDNS denylists and allowlists from the command line";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      versionLine = lib.findFirst (line: lib.hasPrefix "__version__" line) null (
        lib.splitString "\n" (builtins.readFile ./nextdnsctl/__init__.py)
      );
      version = builtins.head (builtins.match ''__version__ = "(.*)"'' versionLine);
    in
    {
      packages = forAllSystems (pkgs: rec {
        nextdnsctl = pkgs.python3Packages.buildPythonApplication {
          pname = "nextdnsctl";
          inherit version;
          pyproject = true;
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./README.md
              ./LICENSE
              ./nextdnsctl
              ./tests
            ];
          };
          build-system = [ pkgs.python3Packages.hatchling ];
          dependencies = with pkgs.python3Packages; [
            click
            requests
            idna
            pyyaml
          ];
          nativeCheckInputs = with pkgs.python3Packages; [
            pytestCheckHook
            pytest-mock
            requests-mock
          ];
          pythonImportsCheck = [ "nextdnsctl" ];
          meta = {
            description = "Bulk-edit, import and back up NextDNS denylists and allowlists from the command line";
            homepage = "https://github.com/danielmeint/nextdnsctl";
            license = lib.licenses.mit;
            mainProgram = "nextdnsctl";
          };
        };
        default = nextdnsctl;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ self.packages.${pkgs.stdenv.hostPlatform.system}.nextdnsctl ];
          packages = [ pkgs.just ];
        };
      });
    };
}
