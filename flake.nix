{
  description = "A Nix-flake-based Python development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = inputs: let
    supportedSystems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
    forEachSupportedSystem = f:
      inputs.nixpkgs.lib.genAttrs supportedSystems (system:
        f {
          pkgs = import inputs.nixpkgs {inherit system;};
        });

    version = "3.13";
  in {
    devShells = forEachSupportedSystem ({pkgs}: let
      concatMajorMinor = v:
        pkgs.lib.pipe v [
          pkgs.lib.versions.splitVersion
          (pkgs.lib.sublist 0 2)
          pkgs.lib.concatStrings
        ];

      python = pkgs."python${concatMajorMinor version}";
    in {
      default = pkgs.mkShell {
        venvDir = ".venv";

        postShellHook = ''
          venvVersionWarn() {
          	local venvVersion
          	venvVersion="$("$venvDir/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null)

          	[[ "$venvVersion" == "${python.version}" ]] && return

          	cat <<EOF
          Warning: Python version mismatch: [$venvVersion (venv)] != [${python.version}]
                   Delete '$venvDir' and reload to rebuild for version ${python.version}
          EOF
          }

          pipInstallWarn() {
          	[[ -f "requirements.txt" ]] || return

          	# Check if venv doesn't exist
          	if [[ ! -d "$venvDir" ]]; then
          		cat <<EOF
          Warning: Python virtual environment not found at '$venvDir'

          Install dependencies by running:
            pip install -r requirements.txt
          EOF
          		return
          	fi

          	# Check if venv is empty (only basic packages installed)
          	local packageCount
          	packageCount=$("$venvDir/bin/pip" list 2>/dev/null | wc -l)
          	if [[ "$packageCount" -le 3 ]]; then
          		cat <<EOF
          Warning: Python virtual environment is empty at '$venvDir'

          Install dependencies by running:
            pip install -r requirements.txt
          EOF
          		return
          	fi

          	# Check if requirements.txt is newer than venv
          	if [[ "requirements.txt" -nt "$venvDir" ]]; then
          		cat <<EOF
          Warning: requirements.txt has been updated since the virtual environment was created

          To update your dependencies, run:
            pip install -r requirements.txt --upgrade
          EOF
          		return
          	fi
          }

          venvVersionWarn
          pipInstallWarn
        '';

        buildInputs = with pkgs; [
          git
          ruff # Linter
          ty # Typechecker
        ];

        packages = with python.pkgs; [
          venvShellHook
          pip
        ];
      };
    });
  };
}
