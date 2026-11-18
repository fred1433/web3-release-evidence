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
    // One order, at most one debit. An order is one payer granting one reader one dataset, for one
    // access period. Its id never depends on a block number or on a count of payments.
    mapping(bytes32 => bool) public orderPaid;
    // Access periods that ended by revocation, per payer, reader and dataset. A revocation closes the
    // period of a paid order; the next grant of the same reader and dataset is then a new order.
    mapping(address => mapping(address => mapping(bytes32 => uint256))) public periodsEnded;

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
    error OrderAlreadyPaid();
    error NotCurrentOrder();

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

    /// The id of the payer's open order for this reader and dataset. It is the same on every device, every
    /// browser and every retry, however many blocks pass, until the order is paid and its grant revoked.
    function orderOf(address payer, address reader, bytes32 dataset) public view returns (bytes32) {
        return keccak256(abi.encode(payer, reader, dataset, periodsEnded[payer][reader][dataset]));
    }

    /// Pays the order `order`. A second payment of the same order is refused, and so is an id that is
    /// not the payer's open order for this reader and dataset: the contract, not the interface, keeps
    /// the rule "one order, at most one debit".
    function grantAndPay(address reader, bytes32 dataset, uint256 amount, bytes32 order) external returns (uint256 grantId) {
        if (!allowlisted[msg.sender]) revert NotAllowlisted();
        if (orderPaid[order]) revert OrderAlreadyPaid();
        if (order != orderOf(msg.sender, reader, dataset)) revert NotCurrentOrder();
        orderPaid[order] = true;
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
        periodsEnded[msg.sender][g.reader][g.dataset] += 1;
        emit Revoked(grantId, msg.sender);
    }
}
