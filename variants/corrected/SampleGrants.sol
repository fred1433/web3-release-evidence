// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

interface ITestToken {
    function transferFrom(address from, address to, uint256 value) external returns (bool);
}

/// @notice Synthetic demo application. A data holder grants a reader access to a
/// fictitious dataset, pays the reader in TEST units, and can revoke the grant.
/// The allowlist is a fictitious demo rule, not a legal or compliance control.
contract SampleGrants {
    address public immutable admin;
    ITestToken public immutable token;
    uint256 public grantCount;
    mapping(address => bool) public allowlisted;
    // One payment per intent: an intent id the payer already used is refused.
    mapping(address => mapping(bytes32 => bool)) public intentPaid;

    struct Grant {
        address owner;
        address reader;
        bytes32 dataset;
        uint256 paid;
        bool active;
    }
    mapping(uint256 => Grant) public grants;

    event Allowlisted(address indexed account, bool allowed);
    event Granted(uint256 indexed grantId, address indexed owner, address indexed reader, bytes32 dataset, uint256 paid);
    event Revoked(uint256 indexed grantId, address indexed owner);

    error NotAdmin();
    error NotAllowlisted();
    error NotGrantOwner();
    error GrantNotActive();
    error IntentAlreadyPaid();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    constructor(ITestToken token_) {
        admin = msg.sender;
        token = token_;
    }

    // Admin only, enforced here and not only in the interface.
    function setAllowlisted(address account, bool allowed) external onlyAdmin {
        allowlisted[account] = allowed;
        emit Allowlisted(account, allowed);
    }

    function grantAndPay(address reader, bytes32 dataset, uint256 amount, bytes32 intent) external returns (uint256 grantId) {
        if (!allowlisted[msg.sender]) revert NotAllowlisted();
        if (intentPaid[msg.sender][intent]) revert IntentAlreadyPaid();
        intentPaid[msg.sender][intent] = true;
        token.transferFrom(msg.sender, reader, amount);
        grantId = ++grantCount;
        grants[grantId] = Grant(msg.sender, reader, dataset, amount, true);
        emit Granted(grantId, msg.sender, reader, dataset, amount);
    }

    function revoke(uint256 grantId) external {
        Grant storage g = grants[grantId];
        if (g.owner != msg.sender) revert NotGrantOwner();
        if (!g.active) revert GrantNotActive();
        g.active = false;
        emit Revoked(grantId, msg.sender);
    }
}
